import json

from blueapi.core import MsgGenerator
from dodal.beamlines import i03
from dodal.devices.attenuator import Attenuator
from dodal.devices.eiger import EigerDetector
from dodal.devices.oav.oav_parameters import OAV_CONFIG_FILE_DEFAULTS, OAVParameters

from hyperion.device_setup_plans.utils import (
    start_preparing_data_collection_then_do_plan,
)
from hyperion.experiment_plans.grid_detect_then_xray_centre_plan import (
    create_devices as full_grid_create_devices,
)
from hyperion.experiment_plans.grid_detect_then_xray_centre_plan import (
    detect_grid_and_do_gridscan,
)
from hyperion.experiment_plans.pin_tip_centring_plan import (
    create_devices as pin_tip_create_devices,
)
from hyperion.experiment_plans.pin_tip_centring_plan import pin_tip_centre_plan
from hyperion.log import LOGGER
from hyperion.parameters.plan_specific.grid_scan_with_edge_detect_params import (
    GridScanWithEdgeDetectInternalParameters,
)
from hyperion.parameters.plan_specific.pin_centre_then_xray_centre_params import (
    PinCentreThenXrayCentreInternalParameters,
)


def create_devices():
    full_grid_create_devices()
    pin_tip_create_devices()


def create_parameters_for_grid_detection(
    pin_centre_parameters: PinCentreThenXrayCentreInternalParameters,
) -> GridScanWithEdgeDetectInternalParameters:
    params_json = json.loads(pin_centre_parameters.json())
    grid_detect_and_xray_centre = GridScanWithEdgeDetectInternalParameters(
        **params_json
    )
    LOGGER.info(
        f"Parameters for grid detect and xray centre: {grid_detect_and_xray_centre}"
    )
    return grid_detect_and_xray_centre


def pin_centre_then_xray_centre_plan(
    parameters: PinCentreThenXrayCentreInternalParameters,
    oav_config_files=OAV_CONFIG_FILE_DEFAULTS,
):
    """Plan that perfoms a pin tip centre followed by an xray centre to completely
    centre the sample"""
    oav_config_files["oav_config_json"] = parameters.experiment_params.oav_centring_file

    yield from pin_tip_centre_plan(
        parameters.experiment_params.tip_offset_microns, oav_config_files
    )
    grid_detect_params = create_parameters_for_grid_detection(parameters)

    backlight = i03.backlight()
    aperture_scattergaurd = i03.aperture_scatterguard()
    detector_motion = i03.detector_motion()
    oav_params = OAVParameters("xrayCentring", **oav_config_files)

    yield from detect_grid_and_do_gridscan(
        grid_detect_params,
        backlight,
        aperture_scattergaurd,
        detector_motion,
        oav_params,
    )


def pin_tip_centre_then_xray_centre(
    parameters: PinCentreThenXrayCentreInternalParameters,
) -> MsgGenerator:
    """Starts preparing for collection then performs the pin tip centre and xray centre"""

    eiger: EigerDetector = i03.eiger()
    attenuator: Attenuator = i03.attenuator()

    eiger.set_detector_parameters(parameters.hyperion_params.detector_params)

    return start_preparing_data_collection_then_do_plan(
        eiger,
        attenuator,
        parameters.hyperion_params.ispyb_params.transmission_fraction,
        pin_centre_then_xray_centre_plan(parameters),
    )
