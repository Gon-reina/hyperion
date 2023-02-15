import bluesky.plan_stubs as bps
from ophyd.status import SubscriptionStatus

from artemis.devices.zebra import (
    DISCONNECT,
    OR1,
    PC_PULSE,
    TTL_DETECTOR,
    TTL_SHUTTER,
    TTL_XSPRESS3,
    I03_axes,
    Zebra,
)

MINIMUM_EXPOSURE_TIME = 0.005


def setup_zebra_for_rotation(
    zebra: Zebra,
    axis: I03_axes = I03_axes.OMEGA,
    start_angle: float = 0,
    scan_width: float = 360,
    exposure_time: float = MINIMUM_EXPOSURE_TIME,
    group: str = "setup_zebra_for_rotation",
    wait: bool = False,
):
    """Set up the Zebra to collect a rotation dataset. Any plan using this is
    responsible for setting the smargon velocity appropriately so that the desired
    image width is achieved with the exposure time given here.

    Parameters:
        axis:           I03 axes enum representing which axis to use for position
                        compare. Currently always omega.
        start_angle:    Position at which the scan should begin, in degrees.
        scan_width:     Total angle through which to collect, in degrees.
        exposure_time:  Time that each image is exposed, in seconds.


    """
    # Sanity check params:
    if exposure_time < MINIMUM_EXPOSURE_TIME:
        raise Exception(
            f"Exposure time {exposure_time} less than allowed minimum of {MINIMUM_EXPOSURE_TIME}."
        )

    # Set gate start
    yield from bps.abs_set(zebra.pc.gate_start, start_angle, group=group)
    # set gate width to total width
    yield from bps.abs_set(zebra.pc.gate_width, scan_width, group=group)
    # Set gate position to be angle of interest
    yield from bps.abs_set(zebra.pc.gate_trigger, axis.value, group=group)

    # Trigger the detector with a pulse, (pulse step set to exposure time?)
    yield from bps.abs_set(zebra.output.out_pvs[TTL_DETECTOR], PC_PULSE, group=group)
    yield from bps.abs_set(zebra.pc.pulse_step, exposure_time, group=group)
    yield from bps.abs_set(zebra.pc.pulse_width, exposure_time / 2, group=group)

    # Trigger the shutter with the gate (from PC_GATE & SOFTIN1 -> OR1)
    yield from bps.abs_set(zebra.output.out_pvs[TTL_SHUTTER], OR1, group=group)

    yield from bps.abs_set(zebra.output.out_pvs[TTL_XSPRESS3], DISCONNECT, group=group)
    yield from bps.abs_set(zebra.output.pulse_1_input, DISCONNECT, group=group)

    if wait:
        bps.wait(group)


def setup_zebra_for_rotation_and_arm(
    zebra: Zebra, group="setup_zebra_for_rotation", wait=False
) -> SubscriptionStatus:
    yield from setup_zebra_for_rotation(zebra, group, wait)

    return zebra.pc.arm()
