"""Application entry point: create the wx.App and show the frame."""

import wx

from .frame import DialsFrame


def main():
    app = wx.App(False)
    frame = DialsFrame()
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
