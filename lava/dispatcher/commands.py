import logging
import os
import sys
import yaml

from lava.tool.command import Command
from lava_dispatcher.action import JobError
from lava_dispatcher.job import ZMQConfig
from lava_dispatcher.log import YAMLLogger
from lava_dispatcher.device import NewDevice
from lava_dispatcher.parser import JobParser


# FIXME: drop outdated arguments, job_data, config and output_dir
def run_pipeline_job(job, validate_only):
    # always validate every pipeline before attempting to run.
    exitcode = 0
    try:
        job.validate(simulate=validate_only)
        if not validate_only:
            exitcode = job.run()
    except (JobError, RuntimeError, TypeError, ValueError):
        import traceback
        traceback.print_exc()
        sys.exit(2)
    if exitcode:
        sys.exit(exitcode)


def is_pipeline_job(filename):
    # FIXME: use the schema once it is available
    if filename.lower().endswith('.yaml') or filename.lower().endswith('.yml'):
        return True
    return False


class dispatch(Command):
    """
    Run test scenarios on virtual and physical hardware
    """

    @classmethod
    def register_arguments(cls, parser):
        super(dispatch, cls).register_arguments(parser)
        parser.add_argument(
            "--oob-fd",
            default=None,
            type=int,
            help="Used internally by LAVA scheduler.")
        parser.add_argument(
            "--output-dir",
            default=None,
            help="Directory to put structured output in.")
        parser.add_argument(
            "--validate", action='store_true',
            help="Just validate the job file, do not execute any steps.")
        parser.add_argument(
            "--job-id", action='store', default=None,
            required=True,
            help=("Set the scheduler job identifier. "
                  "This alters process name for easier debugging"))

        # ZMQ configuration is mandatory for v2 jobs
        parser.add_argument(
            "--socket-addr", default=None,
            help="Address of the ZMQ socket used to send the logs to the master")
        parser.add_argument(
            "--master-cert", default=None,
            help="Master certificate file")
        parser.add_argument(
            "--slave-cert", default=None,
            help="Slave certificate file")

        # Don't put any default value as it has to be defined by the calling process
        parser.add_argument(
            "--target",
            default=None,
            help="Run the job on a specific target device"
        )
        parser.add_argument(
            "--dispatcher",
            default=None,
            help="The dispatcher configuration"
        )
        parser.add_argument(
            "--env-dut-path",
            default=None,
            help="File with environment variables to be exported to the device"
        )
        parser.add_argument(
            "job_file",
            metavar="JOB",
            help="Test scenario file"
        )

    def invoke(self):
        """
        Entry point for lava dispatch, after the arguments have been parsed.
        """

        if os.geteuid() != 0:
            logging.error("You need to be root to run lava-dispatch.")
            exit(1)

        if not is_pipeline_job(self.args.job_file):
            logging.error("v1 jobs are not supported on this instance.")
            exit(1)

        if self.args.oob_fd:
            oob_file = os.fdopen(self.args.oob_fd, 'w')
        else:
            oob_file = sys.stderr

        # config the python logging
        # FIXME: move to lava-tool
        # XXX: this is horrible, but: undo the logging setup lava-tool has
        # done.
        del logging.root.handlers[:]
        del logging.root.filters[:]

        # Pipeline always log as YAML so change the base logger.
        # Every calls to logging.getLogger will now return a YAMLLogger.
        logging.setLoggerClass(YAMLLogger)

        # Set process id based on the job-id
        try:
            from setproctitle import setproctitle
        except ImportError:
            logging.warning(
                ("Unable to import 'setproctitle', "
                 "process name cannot be changed"))
        else:
            setproctitle("lava-dispatch [job: %s]" % self.args.job_id)

        # Load the job file
        job = self.parse_job_file(self.args.job_file)
        job_data = job.parameters

        if self.args.output_dir and not os.path.isdir(self.args.output_dir):
            os.makedirs(self.args.output_dir)

        # TODO: is it still usable?
        if self.args.target is None:
            if 'target' not in job_data:
                logging.error("The job file does not specify a target device. "
                              "You must specify one using the --target option.")
                exit(1)
        else:
            job_data['target'] = self.args.target

        run_pipeline_job(job, self.args.validate)

    def parse_job_file(self, filename):
        """
        Uses the parsed device_config instead of the old Device class
        so it can fail before the Pipeline is made.
        Avoids loading all configuration for all supported devices for every job.
        """
        # Prepare the pipeline from the file using the parser.
        device = None  # secondary connections do not need a device
        if self.args.target:
            device = NewDevice(self.args.target)  # DeviceParser
        parser = JobParser()
        job = None

        # Load the configuration files (this should *not* fail)
        env_dut = None
        if self.args.env_dut_path is not None:
            with open(self.args.env_dut_path, 'r') as f_in:
                env_dut = f_in.read()
        dispatcher_config = None
        if self.args.dispatcher is not None:
            with open(self.args.dispatcher, "r") as f_in:
                dispatcher_config = f_in.read()

        try:
            # Create the ZMQ config
            zmq_config = ZMQConfig(self.args.socket_addr,
                                   self.args.master_cert,
                                   self.args.slave_cert)
            # Generate the pipeline
            with open(filename) as f_in:
                job = parser.parse(f_in, device, self.args.job_id,
                                   zmq_config=zmq_config,
                                   dispatcher_config=dispatcher_config,
                                   output_dir=self.args.output_dir,
                                   env_dut=env_dut)
            # Generate the description
            description = job.describe()
            description_file = os.path.join(self.args.output_dir,
                                            'description.yaml')
            if not os.path.exists(self.args.output_dir):
                os.makedirs(self.args.output_dir, 0o755)
            with open(description_file, 'w') as f_describe:
                f_describe.write(yaml.dump(description))

        except JobError as exc:
            logging.error("Invalid job submission: %s", exc)
            exit(1)
        # FIXME: NewDevice schema needs a validation parser
        # device.check_config(job)
        return job
