"""Run Tk on a dedicated process/main thread and emit one private JSON result."""

import json


def main():
    root = None
    try:
        from tkinter import Tk, filedialog

        root = Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        root.update()
        selected = filedialog.askdirectory(
            parent=root,
            title="Select folder to monitor",
            mustexist=True,
        )
        print(json.dumps({"selected": bool(selected), "path": selected or None}))
        return 0
    except Exception:
        print(json.dumps({"selected": False, "path": None, "unavailable": True}))
        return 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
