#!/usr/bin/env python3

import os
import sys
import argparse
import yaml
from datetime import datetime, timedelta
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from helpers import (
    connect_to_postgres, connect_to_influxdb, try_parsing_time,
    ssim_index_to_db, retrieve_expt_config, get_ssim_index)


# cache of Postgres data: experiment 'id' -> json 'data' of the experiment
expt_id_cache = {}


def collect_ssim(video_acked_results, postgres_cursor):
    # process InfluxDB data
    x = {}
    for pt in video_acked_results['video_acked']:
        expt_id = int(pt['expt_id'])
        expt_config = retrieve_expt_config(expt_id, expt_id_cache,
                                           postgres_cursor)
        # index x by (abr, cc)
        abr_cc = (expt_config['abr'], expt_config['cc'])
        if abr_cc not in x:
            x[abr_cc] = []

        ssim_index = get_ssim_index(pt)
        if ssim_index is not None:
            x[abr_cc].append(ssim_index)

    # calculate average SSIM in dB
    ssim = {}
    for abr_cc in x:
        avg_ssim_index = np.mean(x[abr_cc])
        avg_ssim_db = ssim_index_to_db(avg_ssim_index)
        ssim[abr_cc] = avg_ssim_db

    return ssim


def collect_rebuffer(client_buffer_results, postgres_cursor):
    # process InfluxDB data
    x = {}
    for pt in client_buffer_results['client_buffer']:
        expt_id = int(pt['expt_id'])
        expt_config = retrieve_expt_config(expt_id, expt_id_cache,
                                           postgres_cursor)
        # index x by (abr, cc)
        abr_cc = (expt_config['abr'], expt_config['cc'])
        if abr_cc not in x:
            x[abr_cc] = {}

        # index x[abr_cc] by session
        session = (pt['user'], int(pt['init_id']),
                   pt['channel'], int(pt['expt_id']))
        if session not in x[abr_cc]:
            x[abr_cc][session] = {}
            x[abr_cc][session]['min_play_time'] = None
            x[abr_cc][session]['max_play_time'] = None
            x[abr_cc][session]['min_cum_rebuf'] = None
            x[abr_cc][session]['max_cum_rebuf'] = None

        y = x[abr_cc][session]  # short name

        ts = try_parsing_time(pt['time'])
        cum_rebuf = float(pt['cum_rebuf'])

        if pt['event'] == 'startup':
            y['min_play_time'] = ts
            y['min_cum_rebuf'] = cum_rebuf

        if y['max_play_time'] is None or ts > y['max_play_time']:
            y['max_play_time'] = ts

        if y['max_cum_rebuf'] is None or cum_rebuf > y['max_cum_rebuf']:
            y['max_cum_rebuf'] = cum_rebuf

    # calculate 95th percentile rebuffer rate
    rebuffer = {}
    total_play = {}

    for abr_cc in x:
        abr_cc_play = 0
        rebuf_rate = []

        for session in x[abr_cc]:
            y = x[abr_cc][session]  # short name

            if y['min_play_time'] is None or y['min_cum_rebuf'] is None:
                continue

            sess_play = (y['max_play_time'] - y['min_play_time']).total_seconds()
            sess_rebuf = y['max_cum_rebuf'] - y['min_cum_rebuf']

            # exclude short sessions
            if sess_play < 2:
                continue

            abr_cc_play += sess_play
            rebuf_rate.append(sess_rebuf / sess_play)

        if abr_cc_play == 0:
            sys.exit('Error: {}: total play time is 0'.format(abr_cc))

        rebuffer[abr_cc] = np.percentile(rebuf_rate, 95) * 100  # %
        total_play[abr_cc] = abr_cc_play

    return rebuffer, total_play


def plot_ssim_rebuffer(ssim, rebuffer, total_play, output, days):
    time_str = '%Y-%m-%dT%H'
    curr_ts = datetime.utcnow()
    start_ts = curr_ts - timedelta(days=days)
    curr_ts_str = curr_ts.strftime(time_str)
    start_ts_str = start_ts.strftime(time_str)

    title = ('[{}, {}] (UTC)'
             .format(start_ts_str, curr_ts_str))

    fig, ax = plt.subplots()
    ax.set_title(title)
    ax.set_xlabel('95th percentile rebuffer rate (%)')
    ax.set_ylabel('Average SSIM (dB)')
    ax.grid()

    for abr_cc in ssim:
        abr_cc_str = '{}+{}'.format(*abr_cc)
        if abr_cc not in rebuffer:
            sys.exit('Error: {} does not exist both ssim and rebuffer'
                     .format(abr_cc_str))

        abr_cc_str += '\n({:.1f}h)'.format(total_play[abr_cc] / 3600)

        x = rebuffer[abr_cc]
        y = ssim[abr_cc]
        ax.scatter(x, y)
        ax.annotate(abr_cc_str, (x, y))

    # clamp x-axis to [0, 100]
    xmin, xmax = ax.get_xlim()
    xmin = max(xmin, 0)
    xmax = min(xmax, 100)
    ax.set_xlim(xmin, xmax)
    ax.invert_xaxis()

    fig.savefig(output, dpi=150, bbox_inches='tight')
    sys.stderr.write('Saved plot to {}\n'.format(output))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('yaml_settings')
    parser.add_argument('-o', '--output', required=True)
    parser.add_argument('-d', '--days', type=int, default=1)
    args = parser.parse_args()
    output = args.output
    days = args.days

    if days < 1:
        sys.exit('-d/--days must be a positive integer')

    with open(args.yaml_settings, 'r') as fh:
        yaml_settings = yaml.safe_load(fh)

    # create an InfluxDB client and perform queries
    influx_client = connect_to_influxdb(yaml_settings)

    # query video_acked and client_buffer
    video_acked_results = influx_client.query(
        'SELECT * FROM video_acked WHERE time >= now() - {}d'.format(days))
    client_buffer_results = influx_client.query(
        'SELECT * FROM client_buffer WHERE time >= now() - {}d'.format(days))

    # create a Postgres client and perform queries
    postgres_client = connect_to_postgres(yaml_settings)
    postgres_cursor = postgres_client.cursor()

    # collect ssim and rebuffer
    ssim = collect_ssim(video_acked_results, postgres_cursor)
    rebuffer, total_play = collect_rebuffer(
        client_buffer_results, postgres_cursor)

    if not ssim or not rebuffer:
        sys.exit('Error: no data found in the queried range')

    # plot ssim vs rebuffer
    plot_ssim_rebuffer(ssim, rebuffer, total_play, output, days)

    postgres_cursor.close()


if __name__ == '__main__':
    main()
