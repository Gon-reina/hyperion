from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
import numpy as np
from blueapi.core import MsgGenerator
from bluesky import RunEngine
from bluesky.utils import ProgressBarManager
from dodal.beamlines import i03
from dodal.beamlines.i03 import (
    ApertureScatterguard,
    Attenuator,
    Backlight,
    EigerDetector,
    FastGridScan,
    Flux,
    S4SlitGaps,
    Smargon,
    Synchrotron,
    Undulator,
    Zebra,
)
from dodal.devices.aperturescatterguard import AperturePositions
from dodal.devices.eiger import DetectorParams
from dodal.devices.fast_grid_scan import set_fast_grid_scan_params as set_flyscan_params

import hyperion.log
from hyperion.device_setup_plans.manipulate_sample import move_x_y_z
from hyperion.device_setup_plans.read_hardware_for_setup import read_hardware_for_ispyb
from hyperion.device_setup_plans.setup_zebra import (
    set_zebra_shutter_to_manual,
    setup_zebra_for_gridscan,
)
from hyperion.exceptions import WarningException
from hyperion.external_interaction.callbacks.xray_centre.callback_collection import (
    XrayCentreCallbackCollection,
)
from hyperion.parameters import external_parameters
from hyperion.parameters.beamline_parameters import (
    get_beamline_parameters,
    get_beamline_prefixes,
)
from hyperion.parameters.constants import SIM_BEAMLINE
from hyperion.tracing import TRACER

if TYPE_CHECKING:
    from hyperion.parameters.plan_specific.gridscan_internal_params import (
        GridscanInternalParameters,
    )


class GridscanComposite:
    """A container for all the Devices required for a fast gridscan."""

    def __init__(
        self,
        aperture_positions: AperturePositions = None,
        detector_params: DetectorParams = None,
        fake: bool = False,
    ):
        self.aperture_scatterguard: ApertureScatterguard = i03.aperture_scatterguard(
            fake_with_ophyd_sim=fake, aperture_positions=aperture_positions
        )
        self.backlight: Backlight = i03.backlight(fake_with_ophyd_sim=fake)
        self.eiger: EigerDetector = i03.eiger(
            fake_with_ophyd_sim=fake, params=detector_params
        )
        self.fast_grid_scan: FastGridScan = i03.fast_grid_scan(fake_with_ophyd_sim=fake)
        self.flux: Flux = i03.flux(fake_with_ophyd_sim=fake)
        self.s4_slit_gaps: S4SlitGaps = i03.s4_slit_gaps(fake_with_ophyd_sim=fake)
        self.sample_motors: Smargon = i03.smargon(fake_with_ophyd_sim=fake)
        self.undulator: Synchrotron = i03.undulator(fake_with_ophyd_sim=fake)
        self.synchrotron: Undulator = i03.synchrotron(fake_with_ophyd_sim=fake)
        self.zebra: Zebra = i03.zebra(fake_with_ophyd_sim=fake)
        self.attenuator: Attenuator = i03.attenuator(fake_with_ophyd_sim=fake)


flyscan_xray_centre_composite: GridscanComposite | None = None


def create_devices():
    """Creates the devices required for the plan and connect to them"""
    global flyscan_xray_centre_composite
    prefixes = get_beamline_prefixes()
    hyperion.log.LOGGER.info(
        f"Creating devices for {prefixes.beamline_prefix} and {prefixes.insertion_prefix}"
    )
    aperture_positions = AperturePositions.from_gda_beamline_params(
        get_beamline_parameters()
    )
    hyperion.log.LOGGER.info("Connecting to EPICS devices...")
    flyscan_xray_centre_composite = GridscanComposite(
        aperture_positions=aperture_positions
    )
    hyperion.log.LOGGER.info("Connected.")


def set_aperture_for_bbox_size(
    aperture_device: ApertureScatterguard,
    bbox_size: list[int],
):
    # bbox_size is [x,y,z], for i03 we only care about x
    if bbox_size[0] < 2:
        aperture_size_positions = aperture_device.aperture_positions.MEDIUM
        selected_aperture = "MEDIUM_APERTURE"
    else:
        aperture_size_positions = aperture_device.aperture_positions.LARGE
        selected_aperture = "LARGE_APERTURE"
    hyperion.log.LOGGER.info(
        f"Setting aperture to {selected_aperture} ({aperture_size_positions}) based on bounding box size {bbox_size}."
    )

    @bpp.set_run_key_decorator("change_aperture")
    @bpp.run_decorator(
        md={"subplan_name": "change_aperture", "aperture_size": selected_aperture}
    )
    def set_aperture():
        yield from bps.abs_set(aperture_device, aperture_size_positions)

    yield from set_aperture()


def wait_for_gridscan_valid(fgs_motors: FastGridScan, timeout=0.5):
    hyperion.log.LOGGER.info("Waiting for valid fgs_params")
    SLEEP_PER_CHECK = 0.1
    times_to_check = int(timeout / SLEEP_PER_CHECK)
    for _ in range(times_to_check):
        scan_invalid = yield from bps.rd(fgs_motors.scan_invalid)
        pos_counter = yield from bps.rd(fgs_motors.position_counter)
        hyperion.log.LOGGER.debug(
            f"Scan invalid: {scan_invalid} and position counter: {pos_counter}"
        )
        if not scan_invalid and pos_counter == 0:
            return
        yield from bps.sleep(SLEEP_PER_CHECK)
    raise WarningException("Scan invalid - pin too long/short/bent and out of range")


def tidy_up_plans(fgs_composite: GridscanComposite):
    hyperion.log.LOGGER.info("Tidying up Zebra")
    yield from set_zebra_shutter_to_manual(fgs_composite.zebra)


@bpp.set_run_key_decorator("run_gridscan")
@bpp.run_decorator(md={"subplan_name": "run_gridscan"})
def run_gridscan(
    fgs_composite: GridscanComposite,
    parameters: GridscanInternalParameters,
    md={
        "plan_name": "run_gridscan",
    },
):
    sample_motors = fgs_composite.sample_motors

    # Currently gridscan only works for omega 0, see #
    with TRACER.start_span("moving_omega_to_0"):
        yield from bps.abs_set(sample_motors.omega, 0)

    # We only subscribe to the communicator callback for run_gridscan, so this is where
    # we should generate an event reading the values which need to be included in the
    # ispyb deposition
    with TRACER.start_span("ispyb_hardware_readings"):
        yield from read_hardware_for_ispyb(
            fgs_composite.undulator,
            fgs_composite.synchrotron,
            fgs_composite.s4_slit_gaps,
            fgs_composite.attenuator,
            fgs_composite.flux,
        )

    fgs_motors = fgs_composite.fast_grid_scan

    # TODO: Check topup gate
    yield from set_flyscan_params(fgs_motors, parameters.experiment_params)
    yield from wait_for_gridscan_valid(fgs_motors)

    @bpp.set_run_key_decorator("do_fgs")
    @bpp.run_decorator(md={"subplan_name": "do_fgs"})
    @bpp.contingency_decorator(
        except_plan=lambda e: (yield from bps.stop(fgs_composite.eiger)),
        else_plan=lambda: (yield from bps.unstage(fgs_composite.eiger)),
    )
    def do_fgs():
        yield from bps.wait()  # Wait for all moves to complete
        yield from bps.kickoff(fgs_motors)
        yield from bps.complete(fgs_motors, wait=True)

    # Wait for arming to finish
    yield from bps.wait("ready_for_data_collection")
    yield from bps.stage(fgs_composite.eiger)

    with TRACER.start_span("do_fgs"):
        yield from do_fgs()

    yield from bps.abs_set(fgs_motors.z_steps, 0, wait=False)


@bpp.set_run_key_decorator("run_gridscan_and_move")
@bpp.run_decorator(md={"subplan_name": "run_gridscan_and_move"})
def run_gridscan_and_move(
    fgs_composite: GridscanComposite,
    parameters: GridscanInternalParameters,
    subscriptions: XrayCentreCallbackCollection,
):
    """A multi-run plan which runs a gridscan, gets the results from zocalo
    and moves to the centre of mass determined by zocalo"""

    # We get the initial motor positions so we can return to them on zocalo failure
    initial_xyz = np.array(
        [
            (yield from bps.rd(fgs_composite.sample_motors.x)),
            (yield from bps.rd(fgs_composite.sample_motors.y)),
            (yield from bps.rd(fgs_composite.sample_motors.z)),
        ]
    )

    yield from setup_zebra_for_gridscan(fgs_composite.zebra)

    hyperion.log.LOGGER.info("Starting grid scan")
    yield from run_gridscan(fgs_composite, parameters)

    # the data were submitted to zocalo by the zocalo callback during the gridscan,
    # but results may not be ready, and need to be collected regardless.
    # it might not be ideal to block for this, see #327
    xray_centre, bbox_size = subscriptions.zocalo_handler.wait_for_results(initial_xyz)

    if bbox_size is not None:
        with TRACER.start_span("change_aperture"):
            yield from set_aperture_for_bbox_size(
                fgs_composite.aperture_scatterguard, bbox_size
            )

    # once we have the results, go to the appropriate position
    hyperion.log.LOGGER.info("Moving to centre of mass.")
    with TRACER.start_span("move_to_result"):
        yield from move_x_y_z(fgs_composite.sample_motors, *xray_centre, wait=True)


def flyscan_xray_centre(
    parameters: Any,
) -> MsgGenerator:
    """Create the plan to run the grid scan based on provided parameters.

    The ispyb handler should be added to the whole gridscan as we want to capture errors
    at any point in it.

    Args:
        parameters (FGSInternalParameters): The parameters to run the scan.

    Returns:
        Generator: The plan for the gridscan
    """
    assert flyscan_xray_centre_composite is not None
    flyscan_xray_centre_composite.eiger.set_detector_parameters(
        parameters.hyperion_params.detector_params
    )

    subscriptions = XrayCentreCallbackCollection.from_params(parameters)

    @bpp.subs_decorator(  # subscribe the RE to nexus, ispyb, and zocalo callbacks
        list(subscriptions)  # must be the outermost decorator to receive the metadata
    )
    @bpp.set_run_key_decorator("run_gridscan_move_and_tidy")
    @bpp.run_decorator(  # attach experiment metadata to the start document
        md={
            "subplan_name": "run_gridscan_move_and_tidy",
            "hyperion_internal_parameters": parameters.json(),
        }
    )
    @bpp.finalize_decorator(lambda: tidy_up_plans(flyscan_xray_centre_composite))
    def run_gridscan_and_move_and_tidy(fgs_composite, params, comms):
        yield from run_gridscan_and_move(fgs_composite, params, comms)

    return run_gridscan_and_move_and_tidy(
        flyscan_xray_centre_composite, parameters, subscriptions
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--beamline",
        help="The beamline prefix this is being run on",
        default=SIM_BEAMLINE,
    )
    args = parser.parse_args()

    RE = RunEngine({})
    RE.waiting_hook = ProgressBarManager()
    from hyperion.parameters.plan_specific.gridscan_internal_params import (
        GridscanInternalParameters,
    )

    parameters = GridscanInternalParameters(**external_parameters.from_file())
    subscriptions = XrayCentreCallbackCollection.from_params(parameters)

    create_devices()

    RE(flyscan_xray_centre(parameters))
