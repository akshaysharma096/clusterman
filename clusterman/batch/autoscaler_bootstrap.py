# Copyright 2019 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import subprocess
import time
import traceback
import xmlrpc.client

import colorlog
from yelp_batch.batch import batch_command_line_arguments
from yelp_batch.batch import batch_configure
from yelp_batch.batch_daemon import BatchDaemon

from clusterman.args import add_branch_or_tag_arg
from clusterman.args import add_cluster_arg
from clusterman.args import add_cluster_config_directory_arg
from clusterman.args import add_env_config_path_arg
from clusterman.args import add_pool_arg
from clusterman.args import add_scheduler_arg
from clusterman.batch.util import BatchLoggingMixin
from clusterman.config import get_pool_config_path
from clusterman.config import setup_config
from clusterman.signals.external_signal import setup_signals_environment
from clusterman.tools.rookout import enable_rookout
from clusterman.util import get_autoscaler_scribe_stream
from clusterman.util import setup_logging


class AutoscalerBootstrapException(Exception):
    pass


logger = colorlog.getLogger(__name__)
SUPERVISORD_ADDR = "http://localhost:9001/RPC2"
SUPERVISORD_RUNNING_STATES = ("STARTING", "RUNNING")


def wait_for_process(
    rpc: xmlrpc.client.ServerProxy,
    process_name: str,
    num_procs: int = 1,
    terminal_state: str = "RUNNING",
) -> None:
    logger.info(f"waiting for {process_name} to start")
    while True:
        try:
            states = [
                rpc.supervisor.getProcessInfo(f"{process_name}:{process_name}_{i}")["statename"]
                for i in range(num_procs)
            ]
        except OSError:
            logger.warning(f"Could not talk to supervisord process: {traceback.format_exc()}")
            time.sleep(1)
            continue

        if any(state == "FATAL" for state in states):
            raise AutoscalerBootstrapException(f"Process {process_name} could not start; aborting")
        elif all(state == terminal_state for state in states):
            break
        time.sleep(1)


class AutoscalerBootstrapBatch(BatchDaemon, BatchLoggingMixin):
    notify_emails = ["distsys-compute@yelp.com"]

    @batch_command_line_arguments
    def parse_args(self, parser):
        arg_group = parser.add_argument_group("AutoscalerMonitor options")
        add_cluster_arg(arg_group)
        add_pool_arg(arg_group)
        add_scheduler_arg(arg_group)
        add_env_config_path_arg(arg_group)
        add_cluster_config_directory_arg(arg_group)
        add_branch_or_tag_arg(arg_group)
        arg_group.add_argument(
            "--signal-root-directory",
            default="/code/signals",
            help="location of signal artifacts",
        )

    @batch_configure
    def configure_initial(self) -> None:
        setup_config(self.options)
        self.logger = logger
        self.fetch_proc_count, self.run_proc_count = setup_signals_environment(
            self.options.pool,
            self.options.scheduler,
        )
        watcher_config = {
            self.options.pool: get_pool_config_path(self.options.cluster, self.options.pool, self.options.scheduler)
        }
        self.add_watcher(watcher_config)

    def _get_local_log_stream(self, clog_prefix=None):
        # Ensure that the bootstrap logs go to the same scribe stream as the autoscaler
        return get_autoscaler_scribe_stream(self.options.cluster, self.options.pool, self.options.scheduler)

    def run(self):
        env = os.environ.copy()
        args = env.get("CMAN_ARGS", "")

        # Pass through some arguments from the bootstrap script to the actual autoscaler; we prepend
        # these args in case the user *wanted* to specify something different for the bootstrap and the
        # batch (not sure why anyone would ever do this, but *shrug*) (if the arguments are specified twice
        # argparse will just take the last one in the list).
        if self.options.env_config_path:
            args = f'--env-config-path "{self.options.env_config_path}" ' + args
        if self.options.cluster_config_directory:
            args = f'--cluster-config-directory "{self.options.cluster_config_directory}" ' + args
        env["CMAN_ARGS"] = args
        supervisord_proc = subprocess.Popen(
            '/bin/bash -c "supervisord -c clusterman/supervisord/supervisord.conf | '
            f'tee >(stdin2scribe {self._get_local_log_stream()})"',
            env=env,
            shell=True,
        )
        time.sleep(1)  # Give some time for the process to start
        with xmlrpc.client.ServerProxy(SUPERVISORD_ADDR) as rpc:
            skip_supervisord_cleanup = False
            try:
                wait_for_process(
                    rpc,
                    "fetch_signals",
                    num_procs=self.fetch_proc_count,
                    terminal_state="EXITED",
                )
                rpc.supervisor.startProcessGroup("run_signals")
                wait_for_process(rpc, "run_signals", num_procs=self.run_proc_count)
                rpc.supervisor.startProcess("autoscaler")

                while (
                    self.running
                    and rpc.supervisor.getProcessInfo("autoscaler")["statename"] in SUPERVISORD_RUNNING_STATES
                ):
                    time.sleep(5)
            except KeyboardInterrupt:
                # ctrl-c is propogated to the subprocess so don't do the shutdown call here
                skip_supervisord_cleanup = True
            finally:
                # supervisord won't clean up its child processes if we restart or an exception is thrown
                if not skip_supervisord_cleanup:
                    rpc.supervisor.shutdown()

        logger.info("Shutting down...")
        supervisord_proc.wait()


if __name__ == "__main__":
    setup_logging()
    enable_rookout()
    AutoscalerBootstrapBatch().start()
