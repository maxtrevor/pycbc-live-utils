1#!/usr/bin/env python

# Copyright (C) 2016 Tito Dal Canton
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
Read strain data, apply the standard preparation done in offline CBC searches
(highpass, downsampling, gating, injections etc) and write the result back to a
file. Optionally also write the gating data to a text file. This program can
also be used to generate frames of simulated strain data, with or without
injections.
"""

import logging
import argparse
import pycbc.strain
import pycbc.version
import pycbc.frame
import numpy as np
from numpy.random import default_rng
from pycbc.types import float32, float64
from pycbc.types import TimeSeries


def write_strain(file_name, channel, data):
    logging.info('Writing output strain to %s', file_name)

    if file_name.endswith('.gwf'):
        pycbc.frame.write_frame(file_name, channel, data)
    elif file_name.endswith(('.hdf', '.h5')):
        data.save(file_name, group=channel)
    else:
        raise ValueError('Unknown extension for ' + file_name)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--version", action="version",
                    version=pycbc.version.git_verbose_msg)
parser.add_argument('--output-file', type=str, required=True)
parser.add_argument('--output-precision', type=str,
                    choices=['single', 'double'], default='double',
                    help='Precision of output strain, %(default)s by default')
parser.add_argument('--output-gates-file',
                    help='Save gating info to specified file, in the same '
                         'format as accepted by the --gating-file option')
parser.add_argument('--dyn-range-factor', action='store_true',
                    help='Scale the output strain by a large factor (%f) '
                         'to avoid underflows in subsequent '
                         'calculations' % pycbc.DYN_RANGE_FAC)
parser.add_argument('--low-frequency-cutoff', type=float,
                    help='Provide a low-frequency-cutoff for fake strain. '
                         'This is only needed if fake-strain or '
                         'fake-strain-from-file is used')
parser.add_argument('--frame-duration', metavar='SECONDS', type=int,
                    help='Split the produced data into different frame files '
                         'of the given duration. The output file name should '
                         'contain the strings {start} and {duration}, which '
                         'will be replaced by the start GPS time and duration '
                         'in seconds')

parser.add_argument('--state-vector', type=str,
                    help='Name of state vector channel')
parser.add_argument('--state-vector-good', type=int,
                    help='Value of state vector indicating good data')
parser.add_argument('--state-off-segments', type=str, nargs='+',
                    metavar='START,STOP',
                    help='Segment(s) to be given an off state')

parser.add_argument('--dq-vector', type=str,
                    help='Name of DQ vector channel')
parser.add_argument('--dq-vector-good', type=int,
                    help='Value of DQ vector indicating good data')
parser.add_argument('--dq-bad-times', type=float, nargs='+',
                    metavar='TIME', help='Center time(s) of bad DQ epoch(s)')
parser.add_argument('--dq-bad-pad', type=float,
                    help='Duration of bad DQ epoch(s)')

parser.add_argument('--idq-channel', type=str, 
                    help='Name of idq channel')
parser.add_argument('--random-seed', type=int,
                    help='Random seed used to generate fake idq data')
parser.add_argument('--idq-bad-times', type=float, nargs='+',
                    help='Center time(s) of peaks in iDQ timeseries')
parser.add_argument('--idq-bad-pad', type=float,
                    help='Duration of peaks in iDQ data')



pycbc.strain.insert_strain_option_group(parser)
args = parser.parse_args()

if args.frame_duration is not None and args.frame_duration <= 0:
    parser.error('Frame duration should be positive integer, {} given'.format(args.frame_duration))

logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)

# read and condition strain as pycbc_inspiral would do
strain = pycbc.strain.from_cli(args, dyn_range_fac=pycbc.DYN_RANGE_FAC,
                                   precision=args.output_precision)

# if requested, save the gates while we have them
if args.output_gates_file:
    logging.info('Writing output gates')
    with file(args.output_gates_file, 'wb') as gate_f:
        for k, v in strain.gating_info.items():
            for t, w, p in v:
                gate_f.write('%.4f %.2f %.2f\n' % (t, w, p))

# force strain precision to be as requested
strain = strain.astype(
        float32 if args.output_precision == 'single' else float64)

# unless asked otherwise, revert the dynamic range factor
if not args.dyn_range_factor:
    strain /= pycbc.DYN_RANGE_FAC

out_channel_names = [args.channel_name]
out_timeseries = [strain]

# add state vector

if args.state_vector is not None:
    state_dt = 1. / 16.
    state_size = int(strain.duration / state_dt)
    state_data = np.zeros(state_size, dtype=np.uint32)
    state_data[:] = args.state_vector_good

    state_ts = TimeSeries(state_data, delta_t=state_dt,
                          epoch=strain.start_time)
    state_ts_times = state_ts.sample_times.numpy()

    for ss in args.state_off_segments:
        start, end = map(float, ss.split(','))
        fnz = np.flatnonzero(np.logical_and(state_ts_times >= start,
                                            state_ts_times < end))
        if len(fnz):
            state_ts[fnz[0]:fnz[-1]+1] = 0

    out_channel_names.append(args.state_vector)
    out_timeseries.append(state_ts)

# add DQ vector

if args.dq_vector is not None:
    # generate a fake DQ vector with random occasional vetoes
    dq_dt = 1. / 64.
    dq_size = int(strain.duration / dq_dt)
    dq_data = np.zeros(dq_size, dtype=np.uint32)
    dq_data[:] = args.dq_vector_good

    if args.dq_vector_good == 0:
        # Virgo DQ stream style
        dq_bad = 1
    else:
        # LIGO DQ vector style
        dq_bad = 0

    dq_ts = TimeSeries(dq_data, delta_t=dq_dt,
                       epoch=strain.start_time)
    dt_ts_times = dq_ts.sample_times.numpy()

    for dqt in args.dq_bad_times:
        fnz = np.flatnonzero(abs(dt_ts_times - dqt) < args.dq_bad_pad)
        if len(fnz):
            dq_ts[fnz[0]:fnz[-1]+1] = dq_bad

    out_channel_names.append(args.dq_vector)
    out_timeseries.append(dq_ts)

# add iDQ data

if args.idq_channel is not None:
    #generate a fake idq timeseries
    idq_dt = strain.get_delta_t()
    idq_size = len(strain)
    rng = default_rng(args.random_seed)
    idq_data = rng.standard_normal(idq_size)-1
    
    idq_ts = TimeSeries(idq_data, delta_t=idq_dt,
                        epoch = strain.start_time)
    idq_ts_times = idq_ts.sample_times.numpy()
    
    for idqt in args.idq_bad_times:
        fnz = np.flatnonzero(abs(idq_ts_times - idqt) < args.idq_bad_pad)
        if len(fnz):
            idq_ts[fnz[0]:fnz[-1]+1] += 6
    
    out_channel_names.append(args.idq_channel)
    out_timeseries.append(idq_ts)
    
if args.frame_duration:
    start = args.gps_start_time
    stop = args.gps_end_time
    step = args.frame_duration

    # Last frame duration can be shorter than duration if stop doesn't allow
    for s in range(start, stop, step):
        out_ts = [ts.time_slice(s, s+step if s+step < stop else stop) for ts in out_timeseries]
        complete_fn = args.output_file.format(
                start=s, duration=step if s+step < stop else stop - s)
        write_strain(complete_fn, out_channel_names, out_ts)
else:
    write_strain(args.output_file, out_channel_names, out_timeseries)

logging.info('Done')