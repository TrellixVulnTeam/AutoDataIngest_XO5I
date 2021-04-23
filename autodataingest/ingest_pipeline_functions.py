
'''
These are top-level functions for the major steps in the ingestion pipeline.

They are meant to be called in "main.py"
'''

import sys
from pathlib import Path
from glob import glob
import asyncio
import socket

# fabric handles ssh to cluster running jobs
import fabric
import paramiko

# from .email_notifications.receive_gmail_notifications import (check_for_archive_notification, check_for_job_notification, add_jobtimes)

# from .gsheet_tracker.gsheet_functions import (find_new_tracks, update_track_status,
#                                              update_cell, return_cell)

# from .globus_functions import (transfer_file, transfer_pipeline,
#                                cleanup_source, globus_wait_for_completion)

# from .get_track_info import match_ebid_to_source

# from .download_vlaant_corrections import download_vla_antcorr

# from .ssh_utils import try_run_command, run_command

# from .archive_request import archive_copy_SDM

# # Import dictionary defining the job creation script functions for each
# # cluster.
# from .cluster_configs import JOB_CREATION_FUNCTIONS, CLUSTERADDRS

from autodataingest.email_notifications.receive_gmail_notifications import (check_for_archive_notification, check_for_job_notification, add_jobtimes)

from autodataingest.gsheet_tracker.gsheet_functions import (find_new_tracks, update_track_status,
                                             update_cell, return_cell)

from autodataingest.globus_functions import (transfer_file, transfer_pipeline,
                               cleanup_source, globus_wait_for_completion,
                               transfer_general)

from autodataingest.get_track_info import match_ebid_to_source

from autodataingest.download_vlaant_corrections import download_vla_antcorr

from autodataingest.ssh_utils import try_run_command, run_command

from autodataingest.archive_request import archive_copy_SDM

# Import dictionary defining the job creation script functions for each
# cluster.
from autodataingest.cluster_configs import (JOB_CREATION_FUNCTIONS, CLUSTERADDRS,
                                            ENDPOINT_INFO)

from autodataingest.utils import uniquify, uniquify_folder

class AutoPipeline(object):
    """
    Handler for the processing pipeline stages. Each instance is defined by the
    exection block (EB) ID for each track.

    Each stage is its own function and is meant to run asynchronously.
    """

    def __init__(self, ebid):
        self.ebid = ebid

        self._grab_sheetdata()

        # TODO: add flags that can provide the stage we need to run from.
        # This enables easy restarting of tracks partially processed.


    def _grab_sheetdata(self):
        '''
        Get info from the google sheet. This is needed to allow for restarting at
        different stages.
        '''

        target = return_cell(self.ebid, column=4)
        config = return_cell(self.ebid, column=9)
        track_name = return_cell(self.ebid, column=3)

        if len(target) > 0:
            self.target = target
        else:
            self.target = None

        if len(config):
            self.config = config
        else:
            self.config = None

        if len(track_name):
            self.track_name = track_name
        else:
            self.track_name = None

    def _restart_flag(self):
        '''
        Request a restart on the jobs.
        '''
        return True if return_cell(self.ebid, column=28) == 'TRUE' else False

    @property
    def track_folder_name(self):
        return f"{self.target}_{self.config}_{self.track_name}"

    @property
    def project_code(self):
        if self.track_name is None:
            raise ValueError("The track name could not be found. Cannot find the project code.")

        return self.track_name.split(".")[0]

    async def setup_ssh_connection(self, clustername, user='ekoch',
                                   max_retry_connection=10,
                                   reconnect_waittime=900):
        '''
        Setup and test the ssh connection to the cluster.
        '''

        retry_times = 0
        while True:
            try:
                connect = fabric.Connection(CLUSTERADDRS[clustername],
                                            user=user,
                                            connect_kwargs={'passphrase': globals()['password'] if 'password' in globals() else ""})
                # I'm getting intermittent DNS issues on the CC cloud.
                # This is to handle waiting until the DNS problem goes away
                connect.open()

                break

            except socket.gaierror as e:
                print("Encountering DNS issue with exception {e}")
                print("Waiting to retry connection")
                await asyncio.sleep(reconnect_waittime)

                retry_times += 1

                if retry_times >= max_retry_connection:
                    raise Exception(f"Reached maximum retries to connect to {clustername}")
        # Test the connection:
        if not try_run_command(connect):
            raise ValueError(f"Cannot login to {CLUSTERADDRS[clustername]}. Requires password.")

    async def archive_request_and_transfer(self, archive_kwargs={},
                                     notification_kwargs={'timewindow': 48 * 3600},
                                     sleeptime=600,
                                     clustername='cc-cedar',
                                     do_cleanup=True):
        """
        Step 1.

        Request the data be staged from the VLA archive and transfer to destination via globus.
        """

        ebid = self.ebid

        # First check for an archive notification within the last
        # 48 hr. If one is found, don't re-request the track.
        out = check_for_archive_notification(ebid, **notification_kwargs)

        if out is None:

            print(f'Sending archive request for {ebid}')

            archive_copy_SDM(ebid, **archive_kwargs)

        else:
            print(f"Found recent archive request for {ebid}.")

        update_track_status(ebid, message="Archive download staged",
                            sheetname='20A - OpLog Summary',
                            status_col=1)

        # Wait for the notification email that the data is ready for transfer
        while out is None:
            out = check_for_archive_notification(ebid, **notification_kwargs)

            await asyncio.sleep(sleeptime)

        # We should have the path on AOC and the full MS name
        # from the email.
        path_to_data, track_name = out

        self.track_name = track_name

        # Update track name in sheet:
        update_cell(ebid, track_name,
                    name_col=3,
                    sheetname='20A - OpLog Summary')

        # Scrap the VLA archive for target and config w/ astroquery
        # This will query the archive for the list of targets until the output has a matching EBID.
        target, datasize = match_ebid_to_source(ebid,
                                                targets=['M31', 'M33', 'NGC6822', 'IC10', 'IC1613', 'WLM'],
                                                project_code='20A-346',
                                                verbose=False)

        self.target = target

        print(f"Found target {target} with size {datasize} for {ebid}")

        # Add track target to the sheet
        update_cell(ebid, target, name_col=4,
                    sheetname='20A - OpLog Summary')

        # And the data size
        update_cell(ebid, datasize.rstrip('GB'), name_col=14,
                    sheetname='20A - OpLog Summary')

        # We want to easily track (1) target, (2) config, and (3) track name
        # We'll combine these for our folder names where the data will get placed
        # after transfer from the archive.
        config = return_cell(ebid, column=9)
        self.config = config

        self.track_folder_name = f"{target}_{config}_{track_name}"

        print(f"This track was taken in {config} configuration.")
        print(f"This track can be found in the folder with name {self.track_folder_name}")

        # Do globus transfer:

        print(f"Transferring {self.track_folder_name} to {clustername}.")
        transfer_taskid = transfer_file(track_name, self.track_folder_name,
                                        startnode='nrao-aoc',
                                        endnode=clustername,
                                        wait_for_completion=False)

        self.transfer_taskid = transfer_taskid

        print(f"The globus transfer ID is: {transfer_taskid}")

        update_track_status(ebid,
                            message=f"Data transferred to {clustername}",
                            sheetname='20A - OpLog Summary',
                            status_col=1)

        print(f"Waiting for globus transfer to {clustername} to complete.")
        await globus_wait_for_completion(transfer_taskid)
        print(f"Globus transfer {transfer_taskid} completed!")

        update_cell(ebid, "TRUE", name_col=18,
                    sheetname='20A - OpLog Summary')

        # Remove the data staged at NRAO to avoid exceeding our storage quota
        if do_cleanup:
            print(f"Cleaning up {ebid} on nrao-aoc")
            cleanup_source(track_name, node='nrao-aoc')


    async def setup_for_reduction_pipeline(self, clustername='cc-cedar',
                                           **ssh_kwargs):

        """
        Step 2.

        Create products and setup on the cluster running the reduction.
        This should be the destination requested in `archive_request_and_transfer`.

        1. Tests connection to cluster.
        2. Clones the ReductionPipeline repo
        TODO: Allow setting a version for the pipeline repo.
        3. Updates + transfers offline copies of the antenna positions corrections.
        """


        print(f"Starting connection to {clustername}")

        connect = self.setup_ssh_connection(clustername, **ssh_kwargs)

        # Grab the repo; this is where we can also specify a version number, too
        cd_command = f'cd scratch/VLAXL_reduction/{self.track_folder_name}/'

        print(f"Cloning ReductionPipeline to {clustername} at {cd_command}")

        git_clone_command = 'git clone https://github.com/LocalGroup-VLALegacy/ReductionPipeline.git'
        full_command = f'{cd_command} ; rm -r ReductionPipeline ; {git_clone_command}'
        result = run_command(connect, full_command)

        # Before running any reduction, update the antenna correction files
        # and copy that folder to each folder where the pipeline is run
        print("Downloading updates of antenna corrections to 'VLA_antcorr_tables'")
        download_vla_antcorr(data_folder="VLA_antcorr_tables")

        # Move the antenna correction folder over:
        print(f"Copying antenna corrections to {clustername}")
        result = connect.run(f"{cd_command}/VLA_antcorr_tables || mkdir scratch/VLAXL_reduction/{self.track_folder_name}/VLA_antcorr_tables")
        for file in glob("VLA_antcorr_tables/*.txt"):
            result = connect.put(file, remote=f"scratch/VLAXL_reduction/{self.track_folder_name}/VLA_antcorr_tables/")

        if connect.is_connected:
            connect.close()


    async def initial_job_submission(self,
                                    clustername='cc-cedar',
                                    scripts_dir=Path('reduction_job_scripts/'),
                                    submit_continuum_pipeline=True,
                                    submit_line_pipeline=True,
                                    clusteracct=None,
                                    split_time=None,
                                    continuum_time=None,
                                    line_time=None,
                                    scheduler_cmd="",
                                    **ssh_kwargs):
        """
        Step 3.

        Submit jobs or start the reduction pipeline.

        This has three steps:
        1. Import to MS and continuum/line split
        2. Continuum reduction pipeline
        3. Line reduction pipeline.

        2 and 3 can run concurrently but require 1 to finish first.

        Parameters
        -----------

        """

        print(f"Starting job submission of {self.ebid} on {clustername}.")

        # Create local folder where our job submission scripts will be saved to prior to
        # transfer
        track_scripts_dir = scripts_dir / self.track_folder_name

        if not track_scripts_dir.exists():
            track_scripts_dir.mkdir()

        # Setup connection:
        print(f"Starting connection to {clustername}")
        connect = self.setup_ssh_connection(clustername, **ssh_kwargs)

        # Create 1. job to import and split.
        print(f"Making import/split job file for {self.ebid} or {self.track_folder_name}")

        job_split_filename = f"{self.track_folder_name}_job_import_and_split.sh"

        if (track_scripts_dir / job_split_filename).exists():
            (track_scripts_dir / job_split_filename).unlink()

        # Create the job script.
        print(JOB_CREATION_FUNCTIONS[clustername]['IMPORT_SPLIT'](
                target_name=self.track_folder_name.split('_')[0],
                config=self.track_folder_name.split('_')[1],
                trackname=self.track_folder_name.split('_')[2],
                slurm_kwargs={},
                setup_kwargs={}),
            file=open(track_scripts_dir / job_split_filename, 'a'))

        # Move the job script to the cluster:
        print(f"Moving import/split job file for {self.ebid} to {clustername}")
        result = connect.put(track_scripts_dir / job_split_filename,
                            remote=f'scratch/VLAXL_reduction/{self.track_folder_name}/')

        chdir_cmd = f"cd scratch/VLAXL_reduction/{self.track_folder_name}/"

        if clusteracct is not None:
            acct_str = f"--account={clusteracct}"
        else:
            acct_str = ""

        if split_time is not None:
            time_str = f"--time={split_time}"
        else:
            time_str = ""

        submit_cmd = f"{scheduler_cmd} {acct_str} {time_str} {job_split_filename}"

        print(f"Submitting command: {submit_cmd}")

        try:
            result = run_command(connect, f"{chdir_cmd} && {submit_cmd}")
        except ValueError as exc:
            raise ValueError(f"Failed to submit split job! See stderr: {exc}")

        # Record the job ID so we can check for completion.
        self.importsplit_jobid = result.stdout.replace("\n", '').split(" ")[-1]

        print(f"Submitted import/split job file for {self.ebid} on {clustername} as job {self.importsplit_jobid}")

        update_cell(self.ebid, f"{clustername}:{self.importsplit_jobid}", name_col=20,
                    sheetname='20A - OpLog Summary')


        # Move on to 2. and 3.
        # NEED to make these jobs conditional on 1. finishing.

        if submit_continuum_pipeline:

            print(f"Making continuum pipeline job file for {self.ebid} or {self.track_folder_name}")

            job_continuum_filename = f"{self.track_folder_name}_job_continuum.sh"

            # Remove existing job file if it exists
            if (track_scripts_dir / job_continuum_filename).exists():
                (track_scripts_dir / job_continuum_filename).unlink()

            print(JOB_CREATION_FUNCTIONS[clustername]['CONTINUUM_PIPE'](
                    target_name=self.track_folder_name.split('_')[0],
                    config=self.track_folder_name.split('_')[1],
                    trackname=self.track_folder_name.split('_')[2],
                    slurm_kwargs={},
                    setup_kwargs={},
                    conditional_on_jobnum=self.importsplit_jobid),
                file=open(track_scripts_dir / job_continuum_filename, 'a'))

            # Move the job script to the cluster:
            print(f"Moving continuum pipeline job file for {self.ebid} to {clustername}")
            result = connect.put(track_scripts_dir / job_continuum_filename,
                                remote=f'scratch/VLAXL_reduction/{self.track_folder_name}/')

            if continuum_time is not None:
                time_str = f"--time={continuum_time}"
            else:
                time_str = ""

            submit_cmd = f"{scheduler_cmd} {acct_str} {time_str} {job_continuum_filename}"

            print(f"Submitting command: {submit_cmd}")

            try:
                result = run_command(connect, f"{chdir_cmd} && {submit_cmd}")
            except ValueError as exc:
                raise ValueError(f"Failed to submit continuum pipeline job! See stderr: {exc}")

            # Record the job ID so we can check for completion.
            self.continuum_jobid = result.stdout.replace("\n", '').split(" ")[-1]

            print(f"Submitted continuum pipeline job file for {self.ebid} on {clustername} as job {self.continuum_jobid}")

            update_cell(self.ebid, f"{clustername}:{self.continuum_jobid}", name_col=22,
                        sheetname='20A - OpLog Summary')

        else:
            self.continuum_jobid = None

        if submit_line_pipeline:

            print(f"Making line pipeline job file for {self.ebid} or {self.track_folder_name}")

            job_line_filename = f"{self.track_folder_name}_job_line.sh"

            # Remove existing job file if it exists
            if (track_scripts_dir / job_line_filename).exists():
                (track_scripts_dir / job_line_filename).unlink()

            print(JOB_CREATION_FUNCTIONS[clustername]['LINE_PIPE'](
                    target_name=self.track_folder_name.split('_')[0],
                    config=self.track_folder_name.split('_')[1],
                    trackname=self.track_folder_name.split('_')[2],
                    slurm_kwargs={},
                    setup_kwargs={},
                    conditional_on_jobnum=self.importsplit_jobid),
                file=open(track_scripts_dir / job_line_filename, 'a'))

            # Move the job script to the cluster:
            print(f"Moving line pipeline job file for {self.ebid} to {clustername}")
            result = connect.put(track_scripts_dir / job_line_filename,
                                remote=f'scratch/VLAXL_reduction/{self.track_folder_name}/')

            if line_time is not None:
                time_str = f"--time={line_time}"
            else:
                time_str = ""

            submit_cmd = f"{scheduler_cmd} {acct_str} {time_str} {job_line_filename}"

            print(f"Submitting command: {submit_cmd}")

            try:
                result = run_command(connect, f"{chdir_cmd} && {submit_cmd}")
            except ValueError as exc:
                raise ValueError(f"Failed to submit line pipeline job! See stderr: {exc}")

            # Record the job ID so we can check for completion.
            self.line_jobid = result.stdout.replace("\n", '').split(" ")[-1]

            print(f"Submitted line pipeline job file for {self.ebid} on {clustername} as job {self.line_jobid}")

            update_cell(self.ebid, f"{clustername}:{self.line_jobid}", name_col=24,
                        sheetname='20A - OpLog Summary')

        else:
            self.line_jobid = None

        if connect.is_connected:
            connect.close()

        update_track_status(self.ebid,
                            message=f"Reduction running on {clustername}",
                            sheetname='20A - OpLog Summary',
                            status_col=1)


    async def get_job_notifications(self,
                            importsplit_jobid=None,
                            check_continuum_job=True,
                            continuum_jobid=None,
                            check_line_job=True,
                            line_jobid=None,
                            sleeptime=1800):
        """
        Step 4.

        Check if the pipeline jobs completed correctly.

        If so, and if a manual flagging sheet doesn't exist, produce a new
        google sheet that the manual flagging txt file will be generated from.
        """

        # if IDs are not available, try getting from the gsheet.
        # otherwise, skip checking for those jobs to finish.

        print(f"Checking for job notifications on {self.ebid} or {self.track_folder_name}")

        if importsplit_jobid is None:
            importsplit_jobid = self.importsplit_jobid

            # If still None, pull from the spreadsheet
            if importsplit_jobid is None:
                importsplit_jobid = return_cell(self.ebid, column=20).split(":")[-1]

        if continuum_jobid is None and check_continuum_job:
            continuum_jobid = self.continuum_jobid

            # If still None, pull from the spreadsheet
            if continuum_jobid is None:
                continuum_jobid = return_cell(self.ebid, column=22).split(":")[-1]

        if line_jobid is None and check_line_job:
            line_jobid = self.line_jobid

            # If still None, pull from the spreadsheet
            if line_jobid is None:
                line_jobid = return_cell(self.ebid, column=20).split(":")[-1]

        # If the split job ID is still not defined, something has gone wrong.
        if importsplit_jobid is None or importsplit_jobid == "":
            raise ValueError(f"Unable to identify split job ID for EB: {self.ebid}")

        print(f"Waiting for job notifications on {self.ebid} or {self.track_folder_name}")

        while True:
            # Check for a job completion email and check the final status
            job_check = check_for_job_notification(importsplit_jobid)
            # If None, it isn't done yet!
            if job_check is None:
                await asyncio.sleep(sleeptime)
                continue

            job_status_split, job_runtime =  job_check
            is_done_split = True

            print(f"Found import/split notification for {importsplit_jobid} with status {job_status_split}")

            update_cell(self.ebid, job_status_split, name_col=19,
                        sheetname='20A - OpLog Summary')
            update_cell(self.ebid, job_runtime, name_col=25,
                        sheetname='20A - OpLog Summary')

            break

        # Continuum check
        while True:
            if not check_continuum_job:
                is_done_continuum = False
                break

            job_check = check_for_job_notification(continuum_jobid)

            is_done_continuum = False
            if job_check is None:
                await asyncio.sleep(sleeptime)
                continue

            is_done_continuum = True

            job_status_continuum, job_runtime =  job_check

            print(f"Found continuum notification for {continuum_jobid} with status {job_status_continuum}")

            update_cell(self.ebid, job_status_continuum, name_col=21,
                        sheetname='20A - OpLog Summary')
            update_cell(self.ebid, job_runtime, name_col=26,
                        sheetname='20A - OpLog Summary')

            break

        # Line check
        while True:
            if not check_line_job:
                is_done_line = False
                break

            job_check = check_for_job_notification(line_jobid)
            if job_check is None:
                await asyncio.sleep(sleeptime)
                continue

            is_done_line = True

            job_status_line, job_runtime = job_check

            print(f"Found line notification for {line_jobid} with status {job_status_line}")

            update_cell(self.ebid, job_status_line, name_col=23,
                        sheetname='20A - OpLog Summary')
            update_cell(self.ebid, job_runtime, name_col=27,
                        sheetname='20A - OpLog Summary')

            break

        # Make dictionary for restarting jobs.
        restarts = {'IMPORT_SPLIT': False,
                    'CONTINUUM_PIPE': False,
                    'LINE_PIPE': False,}


        if all([is_done_split, is_done_continuum, is_done_line]):

            # Check if these were successful runs:
            # Expected types of job status:
            # COMPLETED - probably a successful pipeline reduction
            # TIMEOUT - ran out of time; trigger resubmitting the job
            # CANCELLED - something happened to the job. Assumed this was for a good reason and don't resubmit

            # TODO: handle timeout and restart jobs to get the total wall time


            job_statuses = [job_status_split, job_status_continuum, job_status_line]

            # Good! It worked! Move on to QA.
            if all([job_status == 'COMPLETED' for job_status in job_statuses]):

                print(f"Processing complete for {self.ebid}! Ready for QA.")

                update_track_status(self.ebid, message=f"Ready for QA",
                                    sheetname='20A - OpLog Summary',
                                    status_col=1)

            # If the split failed, the other two will not have completed.
            # Trigger resubmitting all three:
            if job_status_split == 'TIMEOUT':
                # Re-add all to submission queue
                print(f"Timeout for split. Needs resubmitting of all jobs")

                restarts['IMPORT_SPLIT'] = True
                restarts['CONTINUUM_PIPE'] = True
                restarts['LINE_PIPE'] = True

            # Trigger resubmitting the continuum
            if job_status_continuum == 'TIMEOUT':
                # Add to resubmission queue
                print(f"Timeout for continuum pipeline. Needs resubmitting of continuum job.")
                restarts['CONTINUUM_PIPE'] = True

            # Trigger resubmitting the lines
            if job_status_line == 'TIMEOUT':
                # Add to resubmission queue
                print(f"Timeout for line pipeline. Needs resubmitting of line job.")
                restarts['LINE_PIPE'] = True

            # Otherwise assume something else went wrong and request a manual review
            if any([job_status not in ['COMPLETED', 'TIMEOUT'] for job_status in job_statuses]):


                print(f"An unhandled issue occured in a job. Needs manual review for {self.ebid}")

                update_track_status(self.ebid,
                                    message=f"ISSUE: Needs manual check of job status",
                                    sheetname='20A - OpLog Summary',
                                    status_col=1)

        else:
            print(f"Not all jobs were run. Needs manual review for {self.ebid}")

            update_track_status(self.ebid,
                                message=f"ISSUE: Not all parts of the reduction were run. Needs manual review.",
                                sheetname='20A - OpLog Summary',
                                status_col=1)

        # TODO: These need to be handled below.
        self.restarts = restarts


    async def restart_job_submission(self, ebid, restart_dictionary):
        """
        Step 3b.

        Resubmit an incomplete job.
        """
        pass


    async def transfer_pipeline_products(self, data_type='speclines',
                                         startnode='cc-cedar',
                                         endnode='ingester'):
        """
        Step 5.

        Transfer pipeline outputs to a storage system the webserver can access to host.
        """

        # Get info from the spreadsheet.

        if not data_type in ['speclines', 'continuum']:
            raise ValueError(f"Data type must be 'speclines' or 'continuum'. Received {data_type}")

        self._grab_sheetdata()

        if self.target is None or self.track_name is None:
            raise ValueError(f"Cannot find target or trackname in {self.ebid}")

        print(f"Transferring {self.track_folder_name} {data_type} products from {startnode} to {endnode}.")

        path_to_products = f'{self.track_folder_name}/{self.track_folder_name}_{data_type}/'

        filename = f'{path_to_products}/{self.track_folder_name}_{data_type}_products.tar'

        # Going to the ingester instance. Doesn't need an extra path.
        output_destination = "/"

        transfer_taskid = transfer_general(filename, output_destination,
                                           startnode=startnode,
                                           endnode=endnode,
                                           wait_for_completion=False)

        self.transfer_taskid = transfer_taskid

        print(f"The globus transfer ID is: {transfer_taskid}")

        print(f"Waiting for globus transfer to {endnode} to complete.")
        await globus_wait_for_completion(transfer_taskid, sleeptime=180)
        print(f"Globus transfer {transfer_taskid} completed!")

    def make_qa_products(self, data_type='speclines',
                         verbose=False):
        '''
        Create the QA products for the QA webserver.
        '''

        import os
        import subprocess
        import shutil

        if not data_type in ['speclines', 'continuum']:
            raise ValueError(f"Data type must be 'speclines' or 'continuum'. Received {data_type}")

        self._grab_sheetdata()

        if self.target is None or self.track_name is None:
            raise ValueError(f"Cannot find target or trackname in {self.ebid}")

        data_path = Path(ENDPOINT_INFO['ingester']['data_path'])


        self.setup_qa_track_path()
        qa_path = self.qa_track_path / data_type

        product_tarname = f"{self.track_folder_name}_{data_type}_products.tar"
        product_file = data_path / product_tarname

        if not os.path.exists(product_file):
            print(f"Unable to find products file at {product_file}")
            return

        # Make a temp folder to extract into:
        temp_path = product_file.with_suffix("")

        if os.path.exists(temp_path):
            shutil.rmtree(temp_path)

        os.mkdir(temp_path)

        # Extract weblog
        task_command = ['tar', '--strip-components=1', '-C',
                        f"{temp_path}", '-xf', f"{product_file}",
                        "products/weblog.tgz"]

        task_weblog1 = subprocess.run(task_command, capture_output=True)

        # Extract cal plots
        task_command = ['tar', '--strip-components=1', '-C',
                        f"{temp_path}", '-xf', f"{product_file}",
                        "products/finalBPcal_txt"]

        task_caltxt = subprocess.run(task_command, capture_output=True)

        # Extract scan plots
        task_command = ['tar', '--strip-components=1', '-C',
                        f"{temp_path}", '-xf', f"{product_file}",
                        "products/scan_plots_txt"]

        task_scantxt = subprocess.run(task_command, capture_output=True)

        cur_dir = os.getcwd()

        os.chdir(temp_path)

        # Extract the weblog
        os.mkdir('weblog')

        task_command = ['tar', '--strip-components=1', '-C',
                        "weblog", '-xf', "weblog.tgz"]

        task_weblog2 = subprocess.run(task_command, capture_output=True)

        if verbose:
            print(f"The extracted files are: {os.listdir()}")

        if os.path.exists('weblog'):
            os.remove('weblog.tgz')

        # Generate the QA products:
        import qaplotter
        # qaplotter.make_all_plots(data_type=data_type)
        qaplotter.make_all_plots()

        # Return the original directory
        os.chdir(cur_dir)

        # Check if the name is already in the qa path:

        new_qa_path = qa_path / os.path.split(temp_path)[-1]
        # Add a unique 1,2,3, etc to make sure the name is unique
        new_qa_path =  uniquify_folder(new_qa_path)

        # Open permission for the webserver to read and access the files
        task_command = ['chmod', '-R', 'o+rx', temp_path]

        task_chmod = subprocess.run(task_command, capture_output=True)
        if verbose:
            print(f"The task was: {task_command}")
            task_chmod_stdout = task_chmod.stdout.decode('utf-8').replace("\n", " ")
            print(f"Stdout: {task_chmod_stdout}")
            task_chmod_stderr = task_chmod.stderr.decode('utf-8').replace("\n", " ")
            print(f"Stderr: {task_chmod_stderr}")

        # Move to the directory of the webserver:
        task_command = ['mv', temp_path, new_qa_path]

        task_move = subprocess.run(task_command, capture_output=True)
        if verbose:
            print(f"The task was: {task_command}")
            task_move_stdout = task_move.stdout.decode('utf-8').replace("\n", " ")
            print(f"Stdout: {task_move_stdout}")
            task_move_stderr = task_move.stderr.decode('utf-8').replace("\n", " ")
            print(f"Stderr: {task_move_stderr}")

        # Now move the tar file to "processed" folder:
        proced_folder = data_path / "processed"
        proced_folder.mkdir(parents=True, exist_ok=True)

        proced_file = uniquify(proced_folder / product_tarname)

        task_command = ['mv', product_file, proced_file]

        task_move = subprocess.run(task_command, capture_output=True)
        if verbose:
            print(f"The task was: {task_command}")
            task_move_stdout = task_move.stdout.decode('utf-8').replace("\n", " ")
            print(f"Stdout: {task_move_stdout}")
            task_move_stderr = task_move.stderr.decode('utf-8').replace("\n", " ")
            print(f"Stderr: {task_move_stderr}")

        # Make tabs in the google sheet for manual flagging:
        # TODO

    @property
    def qa_track_path(self):
        '''
        Location for all QA products on the webserver.
        '''

        qa_path = Path(ENDPOINT_INFO['ingester']['qa_path'])

        return qa_path / self.project_code / self.track_folder_name

    def setup_qa_track_path(self):
        '''
        Create the folder structure that will be ingested into the webserver.

        PROJCODE / TRACKNAME / {continuum/speclines} /

        '''

        self.qa_track_path.mkdir(parents=True, exist_ok=True)
        (self.qa_track_path / 'continuum').mkdir(parents=True, exist_ok=True)
        (self.qa_track_path / 'speclines').mkdir(parents=True, exist_ok=True)

    async def rerun_job_submission(parameter_list):
        """
        Step 7.

        After QA, supplies an additional manual flagging script to re-run the pipeline
        calibration.
        """

        restart_flag = self._restart_flag()

        if not restart_flag:
            print("No restart requested. Exiting")
            return

        # TODO: define what to clean-up from the first pipeline runs.
        # TODO: add in routine to pull in manual flagging scripts. Also backup to a github repo.
        pass


    async def export_track_for_imaging(parameter_list):
        """
        Step 8.

        Move calibrated MSs to a persistent storage location for imaging.
        Clean-up scratch space.
        """
        pass
