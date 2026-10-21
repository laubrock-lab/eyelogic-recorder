"""Boundary paradigm (Rayner, 1975) with PsychoPy and eyelogic-recorder.

Each trial: a fixation cross on the first word must be fixated for 0.5 s; then
a sentence appears with a preview string in place of the target word. When
gaze crosses an invisible boundary just left of the target, the preview is
replaced by the target word. Look at the bottom-left corner (or press SPACE)
to end the trial; Escape quits.

Timing: the changed sentence is drawn into the back buffer *before* the
monitoring loop, so at the crossing nothing is drawn; ``win.flip()`` only
swaps buffers at the next screen refresh (no tearing). The loop only polls gaze.

Messages in the .tsv file per trial: TRIALID n, ITEM, AOI (one per word,
tracker pixels), BOUNDARY, SENTENCE_ONSET, DISPLAY_CHANGE <sample index>,
TRIAL_END.

    python examples/boundary.py
    python examples/boundary.py --simulate --windowed   # mouse = eye, button = blink

Based on the boundary task implemented by Philipp Freiherr von Entreß-Fürsteneck
(University of Potsdam, 2025).
"""

import argparse
import time

from eyelogic_recorder import EyeLogicClient, EyeLogicError, simulator

# (sentence, index of the target word, preview string). Preview and target should
# have the same length so that the monospaced layout does not change. The first
# sentence is from the (public) Potsdam Sentence Corpus (PSC/PSC2); the second
# was written for this demo.
ITEMS = [
    ("Wir hätten schon vor einer Stunde wissen sollen, ob ihr kommt.", 5, "Kammer"),
    ("Am Abend las sie ein Buch über alte Schiffe im Hafen.", 5, "Tuch"),
]
FONT, HEIGHT, GAP = "Courier New", 32, 15


def to_psychopy(x, y, win):
    """Tracker pixels (origin top-left, y down) -> PsychoPy 'pix' (origin centre, y up).

    Tracker coordinates are physical screen pixels; PsychoPy 'pix' units are
    logical pixels, which differ on high-DPI displays (content scale factor).
    On Windows, display scaling must match what the tracker reports.
    """
    f = win.getContentScaleFactor()
    w, h = win.size[0] / f, win.size[1] / f
    return x / f - w / 2, h / 2 - y / f


def to_tracker(px, py, win):
    """Inverse of to_psychopy."""
    f = win.getContentScaleFactor()
    w, h = win.size[0] / f, win.size[1] / f
    return (px + w / 2) * f, (h / 2 - py) * f


def layout_sentence(win, visual, words):
    """One left-anchored TextStim per word, the line centred on screen. Returns stims and AOIs (pix)."""
    stims = [visual.TextStim(win, text=w, font=FONT, height=HEIGHT, color="black",
                             anchorHoriz="left", anchorVert="center") for w in words]
    widths = [s.boundingBox[0] for s in stims]
    x = -(sum(widths) + GAP * (len(words) - 1)) / 2
    aois = []
    for stim, width in zip(stims, widths):
        stim.pos = (x, 0)
        aois.append((x - GAP / 2, HEIGHT * 1.5, x + width + GAP / 2, -HEIGHT * 1.5))  # left, top, right, bottom
        x += width + GAP
    return stims, aois


def wait_for_fixation(et, win, event, poll, pos, duration=0.5, timeout=5.0, radius=60):
    """Show a fixation cross until gaze stays within `radius` px for `duration` s. Returns False on Escape."""
    from psychopy import visual

    visual.TextStim(win, text="+", font=FONT, height=HEIGHT * 2, color="black", pos=pos).draw()
    win.flip()
    t_start, t_on = time.time(), None
    while time.time() - t_start < timeout:
        poll()
        gaze = et.get_position()
        on = gaze is not None and _dist2(to_psychopy(gaze[0], gaze[1], win), pos) <= radius ** 2
        if not on:
            t_on = None  # fixation must be continuous
        elif t_on is None:
            t_on = time.time()
        elif time.time() - t_on >= duration:
            return True
        if "escape" in event.getKeys(["escape"]):
            return False
        time.sleep(0.001)
    # A real experiment would recalibrate here.
    et.send_message("FIXATION_TIMEOUT")
    return True


def run_trial(et, win, event, poll, trial, item, timeout):
    """Returns False if Escape was pressed."""
    from psychopy import visual

    sentence, target_idx, preview = item
    words = sentence.split()
    target = words[target_idx]
    if len(preview) != len(target):
        print("Warning: preview '{}' and target '{}' differ in length".format(preview, target))
    stims, aois = layout_sentence(win, visual, words)
    boundary_x = aois[target_idx][0]  # left edge of the target's AOI = half a gap before the word

    et.check_health()
    et.start_recording()
    try:
        et.send_message("TRIALID {}".format(trial))
        et.send_message("ITEM {} target={} preview={}".format(trial, target, preview))
        for i, (left, top, right, bottom) in enumerate(aois):
            x0, y0 = to_tracker(left, top, win)
            x1, y1 = to_tracker(right, bottom, win)
            et.send_message("AOI {} {:.0f} {:.0f} {:.0f} {:.0f} {}".format(i, x0, y0, x1, y1, words[i]))
        et.send_message("BOUNDARY {:.0f}".format(to_tracker(boundary_x, 0, win)[0]))

        first_word = stims[0].pos[0] + stims[0].boundingBox[0] / 2, 0
        if not wait_for_fixation(et, win, event, poll, first_word):
            return False

        # Sentence with the preview string.
        stims[target_idx].text = preview
        for s in stims:
            s.draw()
        win.callOnFlip(et.send_message, "SENTENCE_ONSET")
        win.flip()

        # Pre-render the changed sentence into the back buffer; the loop below
        # draws nothing, so the change at the crossing is only a buffer swap.
        stims[target_idx].text = target
        for s in stims:
            s.draw()

        crossed = False
        t0 = time.time()
        while time.time() - t0 < timeout:
            poll()
            gaze = et.get_position()  # None during blinks / track loss
            if gaze is not None:
                x, y = to_psychopy(gaze[0], gaze[1], win)
                if not crossed and x > boundary_x:
                    # Logged from inside flip(), at the buffer swap that shows the change.
                    win.callOnFlip(et.send_message, "DISPLAY_CHANGE {}".format(et.last_sample.index))
                    win.flip()
                    crossed = True
                elif crossed and in_bottom_left(x, y, win):
                    break  # looking at the bottom-left corner ends the trial
            keys = event.getKeys(["escape", "space"])
            if "escape" in keys:
                return False
            if "space" in keys:
                break
            # Yield the GIL: the SDK's sample callback needs it to deliver samples
            # promptly. A busy loop without this can delay samples by milliseconds.
            time.sleep(0)
        et.send_message("TRIAL_END")
        win.clearBuffer()  # without a crossing, the back buffer still holds the changed sentence
        win.flip()
        return True
    finally:
        et.stop_recording()


def in_bottom_left(x, y, win):
    f = win.getContentScaleFactor()
    w, h = win.size[0] / f, win.size[1] / f
    return x < -0.4 * w and y < -0.4 * h


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="boundary", help="experiment name (file prefix)")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds before a trial ends by itself")
    parser.add_argument("--windowed", action="store_true", help="window instead of full screen (testing)")
    parser.add_argument("--simulate", action="store_true", help="use the simulated SDK; the mouse acts as the eye")
    parser.add_argument("--no-calibration", action="store_true", help="skip calibration and validation")
    args = parser.parse_args()

    from psychopy import event, visual  # optional dependency, so imported late

    win = None
    state = {"pos": (0, 0), "down": False, "win": None}
    if args.simulate:
        def position():  # runs on the simulator thread: only touches plain data
            if state["win"] is None:  # before the window opens (e.g. during calibration): screen centre
                return (960.0, 540.0)
            if state["down"]:  # button held: blink
                return None
            return to_tracker(state["pos"][0], state["pos"][1], state["win"])
        simulator.install(position=position)

    try:
        with EyeLogicClient(args.name) as et:
            et.connect()
            et.find_tracker()
            et.start_tracking()
            if not args.no_calibration:
                et.calibrate()
                et.validate()

            # Open the window after calibration: the SDK shows its own calibration
            # screen, which a full-screen PsychoPy window could cover.
            win = visual.Window(size=(1280, 720) if args.windowed else (1920, 1080), fullscr=not args.windowed,
                                units="pix", color="white", allowGUI=False)
            mouse = event.Mouse(win=win, visible=args.windowed)
            state["win"] = win

            def poll():  # with --simulate: copy the mouse for the simulator thread
                if args.simulate:
                    state["pos"], state["down"] = mouse.getPos(), mouse.getPressed()[0]

            for trial, item in enumerate(ITEMS, 1):
                if not run_trial(et, win, event, poll, trial, item, args.timeout):
                    break
    except EyeLogicError as err:
        print("EyeLogic error:", err)
        raise SystemExit(1)
    finally:
        if win is not None:
            win.close()


if __name__ == "__main__":
    main()
