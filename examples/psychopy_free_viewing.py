"""Free viewing of images with PsychoPy, recording gaze with eyelogic-recorder.

Each trial shows one image (or a generated stimulus if no images are given)
for a fixed time or until SPACE is pressed. Escape quits. Messages in the
.tsv file: TRIALID n, STIMULUS <name>, IMAGE_ONSET, IMAGE_OFFSET, TRIAL_END.

    python examples/psychopy_free_viewing.py --images photos/ --duration 8
    python examples/psychopy_free_viewing.py --simulate --windowed   # no tracker needed

With --simulate the mouse acts as the eye (hold the left button to blink).
"""

import argparse
import os
import random
import time

from eyelogic_recorder import EyeLogicClient, EyeLogicError, simulator

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".gif")


def to_psychopy(x, y, win):
    """EyeLogic pixels (origin top-left, y down) -> PsychoPy 'pix' (origin centre, y up).

    Assumes the window covers the tracker's screen. On Windows, display
    scaling (magnification) must be 100%, otherwise the two disagree (see the
    EyeLogic SDK documentation).
    """
    f = win.getContentScaleFactor()  # physical vs logical pixels on high-DPI displays
    w, h = win.size[0] / f, win.size[1] / f
    return x / f - w / 2, h / 2 - y / f


def to_eyelogic(px, py, win):
    """Inverse of to_psychopy; feeds the mouse position to the simulator."""
    f = win.getContentScaleFactor()
    w, h = win.size[0] / f, win.size[1] / f
    return (px + w / 2) * f, (h / 2 - py) * f


def collect_images(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.lower().endswith(IMAGE_EXTENSIONS)]
        else:
            files.append(path)
    return files


def make_stimuli(win, images):
    """One stimulus per trial: an ImageStim per file, or random shapes if there are none."""
    from psychopy import visual

    if images:
        stims = []
        for path in images:
            stim = visual.ImageStim(win, image=path)
            scale = min(win.size[0] / stim.size[0], win.size[1] / stim.size[1])  # fit inside window
            stim.size = (stim.size[0] * scale, stim.size[1] * scale)
            stims.append((os.path.basename(path), [stim]))
        return stims
    stims = []
    for n in range(3):
        shapes = []
        for _ in range(12):
            pos = (random.uniform(-0.4, 0.4) * win.size[0], random.uniform(-0.4, 0.4) * win.size[1])
            color = [random.uniform(-1, 1) for _ in range(3)]
            if random.random() < 0.5:
                shapes.append(visual.Circle(win, radius=random.uniform(30, 90), pos=pos, fillColor=color, lineColor=None))
            else:
                shapes.append(visual.Rect(win, width=random.uniform(60, 180), height=random.uniform(60, 180),
                                          pos=pos, fillColor=color, lineColor=None))
        stims.append(("generated{}".format(n + 1), shapes))
    return stims


def run_trial(et, win, event, mouse_state, mouse, trial, name, shapes, duration, show_gaze, marker):
    """Present one stimulus while recording. Returns False if Escape was pressed."""
    et.check_health()  # raises if the SDK or writer thread failed since the last trial
    et.start_recording()
    try:
        et.send_message("TRIALID {}".format(trial))
        et.send_message("STIMULUS {}".format(name))
        event.clearEvents()
        onset = None
        n_invalid = 0
        # Logged from inside flip(), right at the buffer swap that shows the stimulus.
        win.callOnFlip(et.send_message, "IMAGE_ONSET")
        while True:
            mouse_state["pos"] = mouse.getPos()  # read by the simulator's thread
            mouse_state["down"] = mouse.getPressed()[0]
            for s in shapes:
                s.draw()
            gaze = et.get_position()
            if gaze is None:  # blink or track loss: keep the display running
                n_invalid += 1
            elif show_gaze:
                marker.pos = to_psychopy(gaze[0], gaze[1], win)
                marker.draw()
            win.flip()
            if onset is None:
                onset = time.time()
            keys = event.getKeys(["escape", "space"])
            if "escape" in keys:
                return False
            if "space" in keys or time.time() - onset >= duration:
                break
        win.callOnFlip(et.send_message, "IMAGE_OFFSET")
        win.flip()
        et.send_message("TRIAL_END")
        print("Trial {} ({}): {} frames without valid gaze".format(trial, name, n_invalid))
        return True
    finally:
        et.stop_recording()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="free_viewing", help="experiment name (file prefix)")
    parser.add_argument("--images", nargs="*", default=[], help="image files and/or folders; omit for generated stimuli")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per image (SPACE advances earlier)")
    parser.add_argument("--no-shuffle", action="store_true", help="keep the given image order")
    parser.add_argument("--show-gaze", action="store_true", help="draw a gaze marker (for testing)")
    parser.add_argument("--windowed", action="store_true", help="window instead of full screen (testing)")
    parser.add_argument("--simulate", action="store_true", help="use the simulated SDK; the mouse acts as the eye")
    parser.add_argument("--no-calibration", action="store_true", help="skip calibration and validation")
    args = parser.parse_args()

    from psychopy import event, visual  # optional dependency, so imported late

    win = None
    mouse_state = {"pos": (0, 0), "down": False, "win": None}
    if args.simulate:
        def position():  # runs on the simulator thread: only touches plain data
            if mouse_state["win"] is None:  # before the window opens (e.g. during calibration): screen centre
                return (960.0, 540.0)
            if mouse_state["down"]:  # button held: blink
                return None
            return to_eyelogic(mouse_state["pos"][0], mouse_state["pos"][1], mouse_state["win"])
        simulator.install(position=position)

    try:
        with EyeLogicClient(args.name) as et:
            et.connect()
            et.find_tracker()
            et.start_tracking()
            if not args.no_calibration:
                et.calibrate()
                for p in et.validate():
                    print("Validation at ({:.0f}, {:.0f}): left {} deg, right {} deg".format(
                        p.x_px, p.y_px, p.left_deg, p.right_deg))

            # Open the window after calibration: the SDK shows its own calibration
            # screen, which a full-screen PsychoPy window could cover.
            win = visual.Window(size=(1280, 720) if args.windowed else (1920, 1080), fullscr=not args.windowed,
                                units="pix", color="grey", allowGUI=False)
            mouse_state["win"] = win
            mouse = event.Mouse(win=win, visible=args.windowed)

            stimuli = make_stimuli(win, collect_images(args.images))
            if not args.no_shuffle:
                random.shuffle(stimuli)
            marker = visual.Circle(win, radius=15, fillColor="red", lineColor=None)
            for trial, (name, shapes) in enumerate(stimuli, 1):
                if not run_trial(et, win, event, mouse_state, mouse, trial, name, shapes,
                                 args.duration, args.show_gaze, marker):
                    break
    except EyeLogicError as err:
        print("EyeLogic error:", err)
        raise SystemExit(1)
    finally:
        if win is not None:
            win.close()


if __name__ == "__main__":
    main()
