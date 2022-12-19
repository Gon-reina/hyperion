import argparse

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from bluesky import RunEngine
from bluesky.utils import ProgressBarManager

import artemis.log
from artemis.device_setup_plans.setup_zebra_for_fgs import (
    set_zebra_shutter_to_manual,
    setup_zebra_for_fgs,
)
from artemis.devices.eiger import EigerDetector
from artemis.devices.fast_grid_scan import FastGridScan, set_fast_grid_scan_params
from artemis.devices.fast_grid_scan_composite import FGSComposite
from artemis.devices.slit_gaps import SlitGaps
from artemis.devices.synchrotron import Synchrotron
from artemis.devices.undulator import Undulator
from artemis.exceptions import WarningException
from artemis.external_interaction.callbacks import FGSCallbackCollection
from artemis.parameters import ISPYB_PLAN_NAME, SIM_BEAMLINE, FullParameters
from artemis.tracing import TRACER
from artemis.utils import Point3D


def read_hardware_for_ispyb(
    undulator: Undulator,
    synchrotron: Synchrotron,
    slit_gaps: SlitGaps,
):
    artemis.log.LOGGER.debug(
        "Reading status of beamline parameters for ispyb deposition."
    )
    yield from bps.create(
        name=ISPYB_PLAN_NAME
    )  # gives name to event *descriptor* document
    yield from bps.read(undulator.gap)
    yield from bps.read(synchrotron.machine_status.synchrotron_mode)
    yield from bps.read(slit_gaps.xgap)
    yield from bps.read(slit_gaps.ygap)
    yield from bps.save()


@bpp.run_decorator()
def move_xyz(
    sample_motors,
    xray_centre_motor_position,
    md={
        "plan_name": "move_xyz",
    },
):
    """Move 'sample motors' to a specific motor position (e.g. a position obtained
    from gridscan processing results)"""
    yield from bps.mv(
        sample_motors.x,
        xray_centre_motor_position.x,
        sample_motors.y,
        xray_centre_motor_position.y,
        sample_motors.z,
        xray_centre_motor_position.z,
    )


def wait_for_fgs_valid(fgs_motors: FastGridScan, timeout=0.5):
    artemis.log.LOGGER.info("Waiting for valid fgs_params")
    SLEEP_PER_CHECK = 0.1
    times_to_check = int(timeout / SLEEP_PER_CHECK)
    for _ in range(times_to_check):
        scan_invalid = yield from bps.rd(fgs_motors.scan_invalid)
        pos_counter = yield from bps.rd(fgs_motors.position_counter)
        if not scan_invalid and pos_counter == 0:
            return
        yield from bps.sleep(SLEEP_PER_CHECK)
    raise WarningException(f"Scan parameters invalid after {timeout} seconds")


@bpp.run_decorator()
def get_xyz(sample_motors):
    return Point3D(
        (yield from bps.rd(sample_motors.x)),
        (yield from bps.rd(sample_motors.y)),
        (yield from bps.rd(sample_motors.z)),
    )


def tidy_up_plans(fgs_composite: FGSComposite):
    yield from set_zebra_shutter_to_manual(fgs_composite.zebra)


@bpp.run_decorator()
def run_gridscan(
    fgs_composite: FGSComposite,
    eiger: EigerDetector,
    parameters: FullParameters,
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
            fgs_composite.slit_gaps,
        )

    fgs_motors = fgs_composite.fast_grid_scan

    # TODO: Check topup gate
    yield from set_fast_grid_scan_params(fgs_motors, parameters.grid_scan_params)
    yield from wait_for_fgs_valid(fgs_motors)

    @bpp.stage_decorator([eiger, fgs_motors])
    def do_fgs():
        yield from bps.wait()  # Wait for all moves to complete
        yield from bps.kickoff(fgs_motors)
        yield from bps.complete(fgs_motors, wait=True)

    with TRACER.start_span("do_fgs"):
        yield from do_fgs()

    with TRACER.start_span("move_to_z_0"):
        yield from bps.abs_set(fgs_motors.z_steps, 0, wait=False)


def run_gridscan_and_move(
    fgs_composite: FGSComposite,
    eiger: EigerDetector,
    parameters: FullParameters,
    subscriptions: FGSCallbackCollection,
):
    """A multi-run plan which runs a gridscan, gets the results from zocalo
    and moves to the centre of mass determined by zocalo"""

    # We get the initial motor positions so we can return to them on zocalo failure
    initial_xyz = yield from get_xyz(fgs_composite.sample_motors)

    yield from setup_zebra_for_fgs(fgs_composite.zebra)

    # our callbacks should listen to documents only from the actual grid scan
    # so we subscribe to them with our plan
    @bpp.subs_decorator(list(subscriptions))
    def gridscan_with_subscriptions(fgs_composite, detector, params):
        yield from run_gridscan(fgs_composite, detector, params)

    artemis.log.LOGGER.debug("Starting grid scan")
    yield from gridscan_with_subscriptions(fgs_composite, eiger, parameters)

    # the data were submitted to zocalo by the zocalo callback during the gridscan,
    # but results may not be ready, and need to be collected regardless.
    # it might not be ideal to block for this, see #327
    subscriptions.zocalo_handler.wait_for_results(initial_xyz)

    # once we have the results, go to the appropriate position
    artemis.log.LOGGER.info("Moving to centre of mass.")
    with TRACER.start_span("move_to_result"):
        yield from move_xyz(
            fgs_composite.sample_motors,
            subscriptions.zocalo_handler.xray_centre_motor_position,
        )


def get_plan(parameters: FullParameters, subscriptions: FGSCallbackCollection):
    """Create the plan to run the grid scan based on provided parameters.

    Args:
        parameters (FullParameters): The parameters to run the scan.

    Returns:
        Generator: The plan for the gridscan
    """
    artemis.log.LOGGER.info("Fetching composite plan")
    fast_grid_scan_composite = FGSComposite(
        insertion_prefix=parameters.insertion_prefix,
        name="fgs",
        prefix=parameters.beamline,
    )

    # Note, eiger cannot be currently waited on, see #166
    eiger = EigerDetector(
        parameters.detector_params,
        name="eiger",
        prefix=f"{parameters.beamline}-EA-EIGER-01:",
    )

    artemis.log.LOGGER.info("Connecting to EPICS devices...")
    fast_grid_scan_composite.wait_for_connection()
    artemis.log.LOGGER.info("Connected.")

    @bpp.finalize_decorator(lambda: tidy_up_plans(fast_grid_scan_composite))
    def run_gridscan_and_move_and_tidy(fgs_composite, detector, params, comms):
        yield from run_gridscan_and_move(fgs_composite, detector, params, comms)

    return run_gridscan_and_move_and_tidy(
        fast_grid_scan_composite, eiger, parameters, subscriptions
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

    parameters = FullParameters(beamline=args.beamline)
    subscriptions = FGSCallbackCollection.from_params(parameters)

    RE(get_plan(parameters, subscriptions))
