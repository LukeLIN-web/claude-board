"""Stands in for scripts/gpu_hold.py beside the suite's copy of gpu_wait.py (see conftest): a claim
holder that hands its card over imports ``start_bridge`` from here, so no test starts a bridge, a
CUDA context on a real card. Each bridge goes into the file ``$GPU_WAIT_BRIDGED`` names, if set:
``<card> starting``, then ``<card> up`` after ``$GPU_WAIT_BRIDGE_S`` seconds (none unless set)."""
import os
import time


def start_bridge(card: int, by: float) -> bool:
    record = os.environ.get("GPU_WAIT_BRIDGED", os.devnull)
    with open(record, "a") as f:
        f.write(f"{card} starting\n")
    time.sleep(float(os.environ.get("GPU_WAIT_BRIDGE_S", "0")))
    with open(record, "a") as f:
        f.write(f"{card} up\n")
    return True
