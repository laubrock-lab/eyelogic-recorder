"""Paragraph reading with a comprehension question, using PsychoPy and eyelogic-recorder.

Each trial: fixate the cross at the first word for 0.5 s, read the paragraph,
press SPACE when done, then answer a two-choice question with 1 or 2.
Gaze is recorded from the fixation check to the answer. Escape quits.

The paragraph is a TextBox2; word AOIs are derived from its glyph vertices
and logged as messages (tracker pixels), so fixations can be mapped to words
without screenshots. Messages per trial: TRIALID n, ITEM, AOI (one
per word: index, left, top, right, bottom, word), TEXT_ONSET, TEXT_OFFSET,
QUESTION_ONSET, RESPONSE, TRIAL_END.

    python examples/paragraph_reading.py
    python examples/paragraph_reading.py --texts my_texts.csv --screenshots
    python examples/paragraph_reading.py --simulate --windowed   # mouse = eye, button = blink

--texts: CSV with columns id, text, question, option1, option2, correct (1 or 2).
"""

import argparse
import csv
import os
import time

import numpy as np

from eyelogic_recorder import EyeLogicClient, EyeLogicError, simulator

# Public-domain example texts: openings of two fairy tales by the Brothers Grimm,
# verbatim (original spelling) from Kinder- und Haus-Märchen, 7th edition (1857),
# via Wikisource: https://de.wikisource.org/wiki/Die_Sternthaler_(1857) and
# https://de.wikisource.org/wiki/Der_süße_Brei_(1857). Questions written for this demo.
TEXTS = [
    {
        "id": "sternthaler",
        "text": "Es war einmal ein kleines Mädchen, dem war Vater und Mutter gestorben, und es war "
                "so arm, daß es kein Kämmerchen mehr hatte darin zu wohnen und kein Bettchen mehr "
                "darin zu schlafen und endlich gar nichts mehr als die Kleider auf dem Leib und ein "
                "Stückchen Brot in der Hand, das ihm ein mitleidiges Herz geschenkt hatte. Es war "
                "aber gut und fromm.",
        "question": "Was hatte das Mädchen noch?",
        "option1": "die Kleider auf dem Leib und ein Stückchen Brot",
        "option2": "ein Kämmerchen und ein Bettchen",
        "correct": "1",
    },
    {
        "id": "suesser_brei",
        "text": "Es war einmal ein armes frommes Mädchen, das lebte mit seiner Mutter allein, und sie "
                "hatten nichts mehr zu essen. Da gieng das Kind hinaus in den Wald, und begegnete "
                "ihm da eine alte Frau, die wußte seinen Jammer schon und schenkte ihm ein "
                "Töpfchen, zu dem sollt es sagen \u201eTöpfchen, koche,\u201c so kochte es guten süßen "
                "Hirsenbrei, und wenn es sagte \u201eTöpfchen, steh,\u201c so hörte es wieder auf zu kochen.",
        "question": "Was musste das Mädchen sagen, damit das Töpfchen aufhört zu kochen?",
        "option1": "Töpfchen, koche",
        "option2": "Töpfchen, steh",
        "correct": "2",
    },
]
FONT, HEIGHT, LINE_SPACING = "Courier New", 32, 3  # wide line spacing eases assigning fixations to lines


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


def make_paragraph(win, visual, text, size):
    """TextBox2 paragraph centred on screen, plus one AOI per word (left, top, right, bottom in pix).

    TextBox2 has one glyph quad (4 vertices) per character, spaces included, in
    text order. A word is a run of non-space characters; its AOI spans its
    glyphs horizontally, extended to the middle of the gaps to its neighbours,
    and the full line band (line centre +- half the line distance) vertically,
    since glyph boxes only cover the ink.
    """
    text = " ".join(text.split())  # single spaces, no newlines: keeps quads aligned with characters
    box = visual.TextBox2(win, text=text, pos=(0, 0), units="pix", size=size, color="black",
                          font=FONT, letterHeight=HEIGHT, lineSpacing=LINE_SPACING)
    quads = np.asarray(box.verticesPix, dtype=float).reshape(-1, 4, 2)
    if len(quads) != len(text):
        raise RuntimeError("TextBox2 returned {} glyphs for {} characters".format(len(quads), len(text)))

    words, spans, start = [], [], None
    for i, ch in enumerate(text + " "):
        if not ch.isspace() and start is None:
            start = i
        elif ch.isspace() and start is not None:
            words.append(text[start:i])
            spans.append((start, i))
            start = None

    x0 = np.array([quads[a:b, :, 0].min() for a, b in spans])
    x1 = np.array([quads[a:b, :, 0].max() for a, b in spans])
    yc = np.array([np.median(quads[a:b, :, 1].mean(axis=1)) for a, b in spans])
    line = np.zeros(len(words), dtype=int)
    for i in range(1, len(words)):  # a new line starts where the word moves left and down
        line[i] = line[i - 1] + (x0[i] < x0[i - 1] and yc[i] < yc[i - 1] - HEIGHT / 2)
    centres = np.array([np.median(yc[line == k]) for k in range(line.max() + 1)])
    pitch = -np.median(np.diff(centres)) if len(centres) > 1 else HEIGHT * LINE_SPACING
    char_w = np.median([(x1[i] - x0[i]) / len(w) for i, w in enumerate(words)])

    aois = []
    for i in range(len(words)):
        same_prev = i > 0 and line[i - 1] == line[i]
        same_next = i + 1 < len(words) and line[i + 1] == line[i]
        left = (x1[i - 1] + x0[i]) / 2 if same_prev else x0[i] - char_w / 2
        right = (x1[i] + x0[i + 1]) / 2 if same_next else x1[i] + char_w / 2
        c = centres[line[i]]
        aois.append((float(left), float(c + pitch / 2), float(right), float(c - pitch / 2)))
    return box, words, aois


def wait_for_fixation(et, win, event, poll, pos, duration=0.5, timeout=5.0, radius=60):
    """Show a fixation cross until gaze stays within `radius` px for `duration` s. Returns False on Escape."""
    from psychopy import visual

    visual.TextStim(win, text="+", font=FONT, height=HEIGHT * 2, color="black", pos=pos).draw()
    win.flip()
    t_start, t_on = time.time(), None
    while time.time() - t_start < timeout:
        poll()
        gaze = et.get_position()
        if gaze is not None:
            gx, gy = to_psychopy(gaze[0], gaze[1], win)
            on = (gx - pos[0]) ** 2 + (gy - pos[1]) ** 2 <= radius ** 2
        else:
            on = False
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


def wait_for_keys(event, poll, keys):
    """Wait for one of `keys` while keeping the simulator's mouse copy fresh. Returns the key."""
    while True:
        poll()
        pressed = event.getKeys(keys)
        if pressed:
            return pressed[0]
        time.sleep(0.005)


def run_trial(et, win, event, poll, trial, item, size, screenshot_dir):
    """Returns False if Escape was pressed."""
    from psychopy import visual

    box, words, aois = make_paragraph(win, visual, item["text"], size)
    et.check_health()
    et.start_recording()
    try:
        et.send_message("TRIALID {}".format(trial))
        et.send_message("ITEM {}".format(item["id"]))
        for i, ((left, top, right, bottom), word) in enumerate(zip(aois, words)):
            x0, y0 = to_tracker(left, top, win)
            x1, y1 = to_tracker(right, bottom, win)
            et.send_message("AOI {} {:.0f} {:.0f} {:.0f} {:.0f} {}".format(i, x0, y0, x1, y1, word))

        first_word = (aois[0][0] + aois[0][2]) / 2, (aois[0][1] + aois[0][3]) / 2
        if not wait_for_fixation(et, win, event, poll, first_word):
            return False

        # Paragraph
        box.draw()
        win.callOnFlip(et.send_message, "TEXT_ONSET")
        win.flip()
        if screenshot_dir:
            win.getMovieFrame(buffer="front")
            win.saveMovieFrames(os.path.join(screenshot_dir, "{}.png".format(item["id"])))
        t_onset = time.time()
        event.clearEvents()
        if wait_for_keys(event, poll, ["space", "escape"]) == "escape":
            return False
        et.send_message("TEXT_OFFSET")
        reading_time = time.time() - t_onset

        # Question
        question = "{}\n\n1: {}\n2: {}".format(item["question"], item["option1"], item["option2"])
        visual.TextStim(win, text=question, font=FONT, height=HEIGHT, color="black",
                        wrapWidth=size[0], alignText="left").draw()
        win.callOnFlip(et.send_message, "QUESTION_ONSET")
        win.flip()
        t_question = time.time()
        key = wait_for_keys(event, poll, ["1", "2", "num_1", "num_2", "escape"])
        if key == "escape":
            return False
        answer = key[-1]
        correct = answer == str(item["correct"])
        et.send_message("RESPONSE {} correct={} rt={:.3f}".format(answer, int(correct), time.time() - t_question))
        et.send_message("TRIAL_END")
        win.flip()
        print("Trial {} ({}): read {:.1f} s ({:.0f} ms/word), answer {} ({})".format(
            trial, item["id"], reading_time, 1000 * reading_time / len(words), answer,
            "correct" if correct else "wrong"))
        return True
    finally:
        et.stop_recording()


def load_texts(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="paragraph_reading", help="experiment name (file prefix)")
    parser.add_argument("--texts", help="CSV with id, text, question, option1, option2, correct")
    parser.add_argument("--screenshots", action="store_true", help="save each paragraph as PNG next to the data")
    parser.add_argument("--windowed", action="store_true", help="window instead of full screen (testing)")
    parser.add_argument("--simulate", action="store_true", help="use the simulated SDK; the mouse acts as the eye")
    parser.add_argument("--no-calibration", action="store_true", help="skip calibration and validation")
    args = parser.parse_args()

    from psychopy import event, visual  # optional dependency, so imported late

    items = load_texts(args.texts) if args.texts else TEXTS
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
            f = win.getContentScaleFactor()
            size = (0.75 * win.size[0] / f, 0.75 * win.size[1] / f)  # text box, as in the TDW task

            screenshot_dir = None
            if args.screenshots:
                screenshot_dir = os.path.splitext(et.sample_file.name)[0] + "_screenshots"
                os.makedirs(screenshot_dir, exist_ok=True)

            def poll():  # with --simulate: copy the mouse for the simulator thread
                if args.simulate:
                    state["pos"], state["down"] = mouse.getPos(), mouse.getPressed()[0]

            for trial, item in enumerate(items, 1):
                if not run_trial(et, win, event, poll, trial, item, size, screenshot_dir):
                    break
    except EyeLogicError as err:
        print("EyeLogic error:", err)
        raise SystemExit(1)
    finally:
        if win is not None:
            win.close()


if __name__ == "__main__":
    main()
