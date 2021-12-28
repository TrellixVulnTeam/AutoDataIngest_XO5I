
from datetime import datetime, timedelta
import pandas as pd

# from .ssh_utils import run_command
from autodataingest.ssh_utils import run_command


def get_slurm_job_monitor(connect, time_range_days=7, timeout=600):
    '''
    Return job statuses on clusters running slurm.
    '''

    time_now = datetime.now()
    time_week = timedelta(days=time_range_days)

    start_time = time_now - time_week
    start_time_str = start_time.strftime("%Y-%m-%d")

    slurm_cmd = f'sacct --format="JobID,JobName%100,State" --starttime={start_time_str} | grep -v "^[0-9]*\."'

    result = run_command(connect, slurm_cmd, test_connection=False,
                         timeout=timeout)

    # Parse the output into a table.
    lines = result.stdout.split('\n')

    colnames = list(filter(None, lines[0].split(" ")))

    stripped_lines = []

    # Skip line[1]. It's the delimiter in the slurm output
    for ii, line in enumerate(lines[2:]):
        this_line = list(filter(None, line.split(" ")))

        if len(this_line) == 0:
            continue

        # Job num is int
        this_line[0] = int(this_line[0])

        # Strip out track info from the job name
        this_name = this_line[1]
        name_info = this_name.split('-%J')[0].split(".vla_pipeline.")
        if len(name_info) != 2:
            raise ValueError(f"Check job name: {name_info}")

        track_name, job_type = name_info

        ebid = int(track_name.split(".")[2].split('eb')[1])

        this_line.extend([track_name, ebid, job_type])

        stripped_lines.append(this_line)

    colnames.extend(["TrackName", 'EBID', 'JobType'])

    df = pd.DataFrame(stripped_lines, columns=colnames)

    return df


def identify_completions(df_old, df_new):

    diff = df_old.merge(df_new,
                        indicator=True,
                        how='right').loc[lambda x : x['_merge'] != 'both']

    diff_comp = diff[diff['State'] == "COMPLETED"]
    diff_fails = diff[diff['State'] != "COMPLETED"]

    # We also don't need to keep import/split completions, so filter those
    # ones out:
    diff_comp = diff_comp[diff_comp['JobType'] != 'import_and_split']

    return diff_comp, diff_fails
