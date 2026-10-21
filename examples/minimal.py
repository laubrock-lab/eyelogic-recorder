"""Minimal recording without PsychoPy.

Connects, calibrates, validates, then records a few trials of N seconds each,
marking trial boundaries with messages. Prints recording statistics.

    python examples/minimal.py --trials 3 --duration 10
"""

import argparse
import time

from eyelogic_recorder import EyeLogicClient, EyeLogicError, load_samples


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="minimal", help="experiment name (file prefix)")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--duration", type=float, default=10.0, help="seconds per trial")
    parser.add_argument("--frame-rate-mode", type=int, default=0, help="index into the device's frame rates")
    parser.add_argument("--no-calibration", action="store_true", help="skip calibration and validation")
    args = parser.parse_args()

    try:
        with EyeLogicClient(args.name) as et:
            et.connect()
            et.find_tracker()
            et.start_tracking(args.frame_rate_mode)
            print("Screen:", et.get_active_screen())

            if not args.no_calibration:
                et.calibrate()
                for p in et.validate():
                    print("Validation at ({:.0f}, {:.0f}): left {} deg, right {} deg".format(
                        p.x_px, p.y_px, p.left_deg, p.right_deg))

            for trial in range(1, args.trials + 1):
                et.start_recording()
                try:
                    et.send_message("TRIALID {}".format(trial))
                    t_end = time.time() + args.duration
                    while time.time() < t_end:
                        print("\rTrial {}: gaze {}".format(trial, et.get_position()), end="", flush=True)
                        time.sleep(0.1)
                    et.send_message("TRIAL_END")
                finally:
                    et.stop_recording()
                print()

            sample_path = et.sample_file.name
            print("Samples written:", et.samples_written)
            print("Samples dropped (writer fell behind):", et.samples_dropped)
            print("Max writer queue depth:", et.max_queue_depth)
    except EyeLogicError as err:
        print("EyeLogic error:", err)
        raise SystemExit(1)

    data = load_samples(sample_path)
    gaps = int((data["index"][1:] - data["index"][:-1] != 1).sum()) if data.size else 0
    print("Rows in {}: {}, index gaps (incl. between trials): {}".format(sample_path, data.size, gaps))


if __name__ == "__main__":
    main()
