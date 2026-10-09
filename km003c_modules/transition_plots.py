"""PNG plots of native PPS/AVS measurements and estimated transition metrics."""
from pathlib import Path

from .transition_analysis import REQUEST_MESSAGES, STOP_MESSAGES

TRANSITION_PLOT_SUFFIXES = ('.transitions.png', '.transition_timing.png', '.transition_slew.png')


def require_transition_plotting():
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ValueError('Transition plots require matplotlib; install requirements-analysis.txt') from exc
    return plt


def _sample_segments(scope, max_gap_us):
    """Break connectors at duplicate timestamps and measurement gaps."""
    segment = []
    for sample in sorted(scope, key=lambda item: item.timestamp_us):
        if segment and not 0 < sample.timestamp_us - segment[-1].timestamp_us <= max_gap_us:
            yield segment
            segment = []
        segment.append(sample)
    if segment:
        yield segment


def write_transition_plots(analyses, pd, scope, prefix, *, max_gap_us):
    plt = require_transition_plotting()
    from matplotlib.ticker import MaxNLocator
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    origin = min([s.timestamp_us for s in scope] + [a.request_start_us for a in analyses], default=0)
    colors = {'SPR_PPS': '#2764ae', 'SPR_AVS': '#be6511', 'EPR_AVS': '#7c42a8'}
    paths = []

    def save(fig, suffix):
        path = prefix.with_suffix(suffix)
        try:
            fig.savefig(path, dpi=160)
            paths.append(str(path.resolve()))
        finally:
            plt.close(fig)

    fig, axis = plt.subplots(figsize=(12, 5), layout='constrained')
    for index, segment in enumerate(_sample_segments(scope, max_gap_us)):
        axis.plot([(s.timestamp_us-origin)/1e6 for s in segment], [s.vbus_V for s in segment],
                  '.-', color='#222222', linewidth=.8, markersize=3,
                  label='Measured VBUS (native samples)' if index == 0 else None)
    protocols, events = set(), set()
    scope_end = max((s.timestamp_us for s in scope), default=origin)
    for number, result in enumerate(analyses, 1):
        start = (result.request_start_us-origin)/1e6
        raw_index = next((i for i, row in enumerate(pd) if row.row_index == result.request_row_index
                          and row.start_us == result.request_start_us), len(pd))
        end_us = next((row.start_us for row in pd[raw_index+1:]
                       if row.message in REQUEST_MESSAGES | STOP_MESSAGES), scope_end)
        end = max(start, (end_us-origin)/1e6)
        color = colors[result.supply_type]
        label = result.supply_type.replace('_', ' ') + ' target'
        axis.hlines(result.target_voltage_V, start, end, color=color, linestyle='--',
                    linewidth=1.3, label=label if result.supply_type not in protocols else None)
        band = abs(result.target_voltage_V) * result.target_band_fraction
        axis.fill_between([start, end], result.target_voltage_V-band, result.target_voltage_V+band,
                          color=color, alpha=.08)
        protocols.add(result.supply_type)
        for name, stamp, style, event_color in (
            ('Request', result.request_start_us, ':', '#555555'),
            ('ACCEPT', result.accept_start_us, ':', '#c38a00'),
            ('PS_RDY', result.ps_rdy_start_us, '-.', '#16824b')):
            if stamp is not None:
                axis.axvline((stamp-origin)/1e6, color=event_color, linestyle=style, alpha=.55,
                             linewidth=.8, label=name if name not in events else None)
                events.add(name)
        # Avoid covering the waveform with labels in long captures.
        if len(analyses) <= 30:
            axis.annotate(str(number), (start, result.target_voltage_V), xytext=(3, 5),
                          textcoords='offset points', color=color, fontsize=8)
    if not analyses:
        axis.text(.5, .95, 'No decodable PPS/AVS requests', ha='center', va='top', transform=axis.transAxes)
    axis.set(xlabel='Device time from first plotted event (s)', ylabel='VBUS (V)',
             title='KM003C PPS / AVS transitions')
    axis.grid(alpha=.25)
    if axis.get_legend_handles_labels()[0]:
        axis.legend(fontsize=8, loc='best', ncols=2)
    fig.supxlabel('Native samples only; connectors are visual guides. Gaps are not interpolated.', fontsize=9)
    save(fig, TRANSITION_PLOT_SUFFIXES[0])

    x = list(range(1, len(analyses)+1))
    fig, axis = plt.subplots(figsize=(12, 5), layout='constrained')
    for field, label, marker in (
        ('accept_latency_us', 'Request to ACCEPT', 'o'),
        ('ps_rdy_latency_us', 'Request to PS_RDY', 's'),
        ('movement_latency_us', 'Request to measured movement', '^'),
        ('settling_latency_us', 'Request to target-band settling', 'D'),
        ('observed_settling_latency_us', 'Request to observed-plateau settling', 'x')):
        values = [(n, getattr(a, field)/1000) for n, a in zip(x, analyses) if getattr(a, field) is not None]
        if values:
            axis.scatter(*zip(*values), label=label, marker=marker, s=32)
    axis.set(xlabel='Transition number (capture order)', ylabel='Latency (ms)',
             title='KM003C PPS / AVS response and estimated settling times')
    axis.xaxis.set_major_locator(MaxNLocator(integer=True))
    axis.grid(alpha=.25)
    if axis.get_legend_handles_labels()[0]:
        axis.legend(fontsize=8)
    else:
        axis.text(.5, .5, 'No available timing measurements', ha='center', transform=axis.transAxes)
    fig.supxlabel('KM003C: 1 ms device timestamps, normally 40 ms polling. Not a compliance test.', fontsize=9)
    save(fig, TRANSITION_PLOT_SUFFIXES[1])

    fig, axis = plt.subplots(figsize=(12, 5), layout='constrained')
    for field, label, marker in (
        ('average_slew_V_per_s', 'To requested target band', 'o'),
        ('observed_average_slew_V_per_s', 'To observed plateau band', 'x')):
        values = [(n, getattr(a, field)) for n, a in zip(x, analyses) if getattr(a, field) is not None]
        if values:
            axis.scatter(*zip(*values), label=label, marker=marker, s=35)
    axis.axhline(0, color='#888888', linewidth=.6)
    axis.set(xlabel='Transition number (capture order)', ylabel='Estimated average slew (V/s)',
             title='KM003C PPS / AVS estimated slew rates')
    axis.xaxis.set_major_locator(MaxNLocator(integer=True))
    axis.grid(alpha=.25)
    if axis.get_legend_handles_labels()[0]:
        axis.legend(fontsize=8)
    else:
        axis.text(.5, .5, 'No resolved slew rates', ha='center', transform=axis.transAxes)
    fig.supxlabel('Positive = rising; negative = falling. Unresolved values are omitted.', fontsize=9)
    save(fig, TRANSITION_PLOT_SUFFIXES[2])
    return paths
