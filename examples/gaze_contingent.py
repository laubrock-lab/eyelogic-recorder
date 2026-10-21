"""Gaze-contingent moving window with PsychoPy: reading or scene viewing.

A red dot follows gaze (--no-marker hides it). Around the gaze position a
window shows the stimulus normally; outside the window it is masked:

  --mode text   reading: letters outside +-N characters on the fixated line are
                replaced by x (spaces kept), as in the classic moving-window
                paradigm (McConkie & Rayner, 1975); --window N (default 7)
  --mode scene  scene viewing: outside a circular window the scene is covered
                by grey; --radius in pixels (default 150); --images for your own

The window is drawn with a PsychoPy Aperture (stencil mask), so the stimulus is
not rebuilt when gaze moves. SPACE ends a trial, Escape quits. Messages:
TRIALID n, ITEM, WINDOW, STIMULUS_ONSET, TRIAL_END. For the boundary
paradigm, see boundary.py.

    python examples/gaze_contingent.py --mode text
    python examples/gaze_contingent.py --mode scene --images photos/
    python examples/gaze_contingent.py --simulate --windowed   # mouse = eye, button = blink
"""

import argparse
import os
import random
import textwrap
import time

from eyelogic_recorder import EyeLogicClient, EyeLogicError, simulator

# Public domain: openings of two Grimm fairy tales (Kinder- und Haus-Märchen, 1857),
# verbatim via Wikisource (see paragraph_reading.py).
TEXTS = [
    ("sternthaler", "Es war einmal ein kleines Mädchen, dem war Vater und Mutter gestorben, und es war "
                    "so arm, daß es kein Kämmerchen mehr hatte darin zu wohnen und kein Bettchen mehr "
                    "darin zu schlafen und endlich gar nichts mehr als die Kleider auf dem Leib und ein "
                    "Stückchen Brot in der Hand, das ihm ein mitleidiges Herz geschenkt hatte."),
    ("suesser_brei", "Es war einmal ein armes frommes Mädchen, das lebte mit seiner Mutter allein, und sie "
                     "hatten nichts mehr zu essen. Da gieng das Kind hinaus in den Wald, und begegnete "
                     "ihm da eine alte Frau, die wußte seinen Jammer schon und schenkte ihm ein Töpfchen."),
]
FONT, HEIGHT, LINE_SPACING, CHARS_PER_LINE = "Courier New", 28, 2.5, 60
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".gif")


def to_psychopy(x, y, win):
    """Tracker pixels (origin top-left, y down) -> PsychoPy 'pix' (origin centre, y up).

    Tracker coordinates are physical screen pixels; PsychoPy 'pix' units are
    logical pixels, which differ on high-DPI displays (content scale factor).
    Conversion by Philipp Freiherr von Entreß-Fürsteneck.
    """
    f = win.getContentScaleFactor()
    w, h = win.size[0] / f, win.size[1] / f
    return x / f - w / 2, h / 2 - y / f


def to_tracker(px, py, win):
    """Inverse of to_psychopy; feeds the mouse position to the simulator."""
    f = win.getContentScaleFactor()
    w, h = win.size[0] / f, win.size[1] / f
    return (px + w / 2) * f, (h / 2 - py) * f


class TextWindow:
    """Monospaced text, masked outside +-n characters around gaze on the fixated line."""

    def __init__(self, win, visual, text, n):
        self.n = n
        self.char_w = visual.TextStim(win, text="x", font=FONT, height=HEIGHT).boundingBox[0]
        width = win.size[0] / win.getContentScaleFactor()
        per_line = min(CHARS_PER_LINE, int(0.9 * width // self.char_w))  # fit narrow windows
        lines = textwrap.wrap(text, per_line)
        masked = ["".join("x" if c != " " else " " for c in line) for line in lines]
        self.pitch = HEIGHT * LINE_SPACING
        self.left = -per_line * self.char_w / 2
        self.top = (len(lines) - 1) * self.pitch / 2
        self.n_lines = len(lines)

        def stims(strings):
            # One TextStim per line, never wrapped by PsychoPy itself (its default wrap
            # width would break long lines and centre the continuation).
            return [visual.TextStim(win, text=s, font=FONT, height=HEIGHT, color="black",
                                    anchorHoriz="left", anchorVert="center", alignText="left",
                                    wrapWidth=10 * width, pos=(self.left, self.top - i * self.pitch))
                    for i, s in enumerate(strings)]

        self.full, self.masked = stims(lines), stims(masked)
        self.background = visual.Rect(win, width=win.size[0], height=win.size[1], fillColor="white", lineColor=None)

    def draw_masked(self):
        for s in self.masked:
            s.draw()

    def window(self, x, y):
        """Aperture (pos, size) for gaze at (x, y), snapped to whole characters; None if off the text."""
        line = round((self.top - y) / self.pitch)
        if not 0 <= line < self.n_lines:
            return None
        char = int((x - self.left) // self.char_w)
        x0 = self.left + (char - self.n) * self.char_w
        width = (2 * self.n + 1) * self.char_w
        return (x0 + width / 2, self.top - line * self.pitch), (width, self.pitch)

    def draw_full(self):
        self.background.draw()  # cover the masked letters inside the window
        for s in self.full:
            s.draw()


class SceneWindow:
    """A scene (image or generated shapes), greyed out outside a circular window around gaze."""

    def __init__(self, win, visual, shapes, radius):
        self.shapes = shapes
        self.radius = radius
        self.cover = visual.Rect(win, width=win.size[0], height=win.size[1], fillColor="grey",
                                 lineColor=None, opacity=0.85)
        self.background = visual.Rect(win, width=win.size[0], height=win.size[1], fillColor="white",
                                      lineColor=None)  # the window is a bright disc over the greyed scene

    def draw_masked(self):
        for s in self.shapes:
            s.draw()
        self.cover.draw()

    def window(self, x, y):
        return (x, y), (2 * self.radius, 2 * self.radius)

    def draw_full(self):
        self.background.draw()
        for s in self.shapes:
            s.draw()


def make_scenes(win, visual, paths):
    """One list of stimuli per scene: an ImageStim per file, or random shapes if there are none."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.lower().endswith(IMAGE_EXTENSIONS)]
        else:
            files.append(path)
    if files:
        scenes = []
        for path in files:
            stim = visual.ImageStim(win, image=path)
            scale = min(win.size[0] / stim.size[0], win.size[1] / stim.size[1])
            stim.size = (stim.size[0] * scale, stim.size[1] * scale)
            scenes.append((os.path.basename(path), [stim]))
        return scenes
    scenes = []
    for n in range(2):
        shapes = []
        for _ in range(15):
            pos = (random.uniform(-0.4, 0.4) * win.size[0], random.uniform(-0.4, 0.4) * win.size[1])
            color = [random.uniform(-1, 1) for _ in range(3)]
            if random.random() < 0.5:
                shapes.append(visual.Circle(win, radius=random.uniform(30, 90), pos=pos, fillColor=color, lineColor=None))
            else:
                shapes.append(visual.Rect(win, width=random.uniform(60, 180), height=random.uniform(60, 180),
                                          pos=pos, fillColor=color, lineColor=None))
        scenes.append(("generated{}".format(n + 1), shapes))
    return scenes


def run_trial(et, win, event, poll, aperture, marker, show_marker, trial, name, stimulus, window_msg, timeout):
    """Returns False if Escape was pressed."""
    et.check_health()
    et.start_recording()
    try:
        et.send_message("TRIALID {}".format(trial))
        et.send_message("ITEM {}".format(name))
        et.send_message(window_msg)
        win.callOnFlip(et.send_message, "STIMULUS_ONSET")
        event.clearEvents()
        t0 = time.time()
        while time.time() - t0 < timeout:
            poll()
            gaze = et.get_position()  # None during blinks / track loss: everything stays masked
            stimulus.draw_masked()
            if gaze is not None:
                x, y = to_psychopy(gaze[0], gaze[1], win)
                window = stimulus.window(x, y)
                if window is not None:
                    aperture.size = window[1]
                    aperture.pos = window[0]  # moving the aperture also enables it
                    stimulus.draw_full()
                    aperture.enabled = False
                if show_marker:
                    marker.pos = (x, y)
                    marker.draw()
            win.flip()
            keys = event.getKeys(["escape", "space"])
            if "escape" in keys:
                return False
            if "space" in keys:
                break
        et.send_message("TRIAL_END")
        return True
    finally:
        et.stop_recording()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="gaze_contingent", help="experiment name (file prefix)")
    parser.add_argument("--mode", choices=("text", "scene"), default="text")
    parser.add_argument("--window", type=int, default=7, help="text mode: characters visible left and right of gaze")
    parser.add_argument("--radius", type=float, default=150, help="scene mode: window radius in pixels")
    parser.add_argument("--images", nargs="*", default=[], help="scene mode: image files and/or folders")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds before a trial ends by itself")
    parser.add_argument("--no-marker", action="store_true", help="do not draw the gaze marker")
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
            # screen, which a full-screen PsychoPy window could cover. The stencil
            # buffer is needed for the Aperture.
            win = visual.Window(size=(1280, 720) if args.windowed else (1920, 1080), fullscr=not args.windowed,
                                units="pix", color="white" if args.mode == "text" else "grey",
                                allowGUI=False, allowStencil=True)
            mouse = event.Mouse(win=win, visible=args.windowed)
            state["win"] = win
            aperture = visual.Aperture(win, size=1, shape=[(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]
                                       if args.mode == "text" else "circle")
            aperture.enabled = False  # an Aperture is enabled when created
            marker = visual.Circle(win, radius=8, fillColor="red", lineColor=None)

            def poll():  # with --simulate: copy the mouse for the simulator thread
                if args.simulate:
                    state["pos"], state["down"] = mouse.getPos(), mouse.getPressed()[0]

            if args.mode == "text":
                items = [(name, TextWindow(win, visual, text, args.window)) for name, text in TEXTS]
                window_msg = "WINDOW {} characters left and right".format(args.window)
            else:
                items = [(name, SceneWindow(win, visual, shapes, args.radius))
                         for name, shapes in make_scenes(win, visual, args.images)]
                window_msg = "WINDOW radius {:.0f} px".format(args.radius)

            for trial, (name, stimulus) in enumerate(items, 1):
                if not run_trial(et, win, event, poll, aperture, marker, not args.no_marker,
                                 trial, name, stimulus, window_msg, args.timeout):
                    break
    except EyeLogicError as err:
        print("EyeLogic error:", err)
        raise SystemExit(1)
    finally:
        if win is not None:
            win.close()


if __name__ == "__main__":
    main()
