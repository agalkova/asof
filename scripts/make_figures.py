"""Draw the revision history of August 2008 payroll employment.

Reads docs/figures/data/payems-2008-08-revisions.csv, the 16 published values
of that observation as returned by notebooks/revision_history.ipynb, and saves
the figure as SVG in docs/figures/ (a wide and a narrow version, drawn at the
pixel width they are shown at).

    python scripts/make_figures.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figure_style as fs
from figure_style import plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "figures" / "data" / "payems-2008-08-revisions.csv"
OUT = ROOT / "docs" / "figures"


def revision_figure(narrow: bool) -> None:
    frame = pd.read_csv(DATA, parse_dates=["vintage_date"])
    fig = fs.new_figure(4.2, 6.0)
    axis = fig.add_axes([0.16 if narrow else 0.11, 0.16, 0.8 if narrow else 0.85, 0.6])
    size = plt.rcParams["font.size"]

    # The value stays in force until the next release, so draw it as steps
    end = pd.Timestamp("2026-08-01")
    dates = list(frame["vintage_date"]) + [end]
    values = list(frame["value_thousands"]) + [frame["value_thousands"].iloc[-1]]
    axis.step(dates, values, where="post", color=fs.BLUE, lw=1.8, zorder=2)
    axis.scatter(
        frame["vintage_date"],
        frame["value_thousands"],
        s=22,
        color=fs.BLUE,
        edgecolor="white",
        linewidth=0.8,
        zorder=3,
    )

    first = frame.iloc[0]
    low = frame.loc[frame["value_thousands"].idxmin()]
    last = frame.iloc[-1]
    notes = [
        (first, f"First published\n{first['value_thousands']:,.0f}", (10, 4), "left"),
        (low, f"Feb 2010: {low['value_thousands']:,.0f}", (4, -14), "left"),
        (last, f"Today: {last['value_thousands']:,.0f}", (0, 14), "right"),
    ]
    for row, text, offset, align in notes:
        axis.annotate(
            text,
            (row["vintage_date"], row["value_thousands"]),
            xytext=offset,
            textcoords="offset points",
            ha=align,
            va="center",
            fontsize=size - 1,
            color=fs.INK,
        )

    axis.set_ylim(136600, 137600)
    axis.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    axis.set_xlim(pd.Timestamp("2008-06-01"), end)
    if narrow:
        import matplotlib.dates as mdates

        axis.xaxis.set_major_locator(mdates.YearLocator(4))
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axis.set_xlabel("Release date")
    if not narrow:
        axis.set_ylabel("Thousands of jobs")
    fs.header(
        fig,
        1,
        "August 2008 payrolls, as published over time",
        None
        if narrow
        else "Each point is one release of the same observation (PAYEMS, thousands of jobs)",
        narrow,
    )
    fs.source(fig, "docs/figures/data/payems-2008-08-revisions.csv", narrow)
    fs.save(
        fig,
        OUT / f"payems-revisions{fs.suffix()}.svg",
        "August 2008 payroll employment as published over time",
        "The first published value was 137,473 thousand jobs in September 2008. Revisions lowered it to "
        "136,741 thousand by February 2010, then raised it again; the current value, published in "
        "February 2025, is 137,211 thousand.",
    )


def main() -> None:
    global OUT
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plain",
        type=Path,
        metavar="DIR",
        help="write versions without the figure label, title and source line to DIR",
    )
    args = parser.parse_args()
    if args.plain:
        fs.PLAIN = True
        OUT = args.plain
    variants = ("wide", "mid", "narrow") if args.plain else ("wide", "narrow")
    for variant in variants:
        fs.VARIANT = variant
        revision_figure(variant != "wide")


if __name__ == "__main__":
    main()
