# -*- coding: utf-8 -*-

import os
import logging
import threading

from twisted.python.failure import Failure

from lie_workflow.workflow_common import WorkflowError, validate_workflow
from lie_workflow.workflow_spec import WorkflowSpec

from twisted.logger import Logger
logging = Logger()


class WorkflowRunner(WorkflowSpec):
    """
    This is the main class for running microservice oriented workflows.

    Running a workflow is based on a workflow specification build using the
    `WorkflowSpec` class. Such a workflow can be loaded into the `Workflow`
    class simply by overloading the Workflow.workflow attribute.
    The `Workflow` class also inherits the methods from the `WorkflowSpec`
    class allowing to build specification right from the `Workflow` class
    and even change a running workflow.

    The execution of workflow steps is performed on a different thread than
    the main Workflow object allowing the user to interact with the running
    workflow.
    The DAG including the metadata dat is generated while executing the steps
    can be serialized to JSON for persistent storage. The same JSON object is
    used as input to the Workflow class validated by a Workflow JSON schema.

    :param workflow:   the workflow DAG to run
    :type workflow:    JSON object
    """

    def __init__(self, workflow=None, **kwargs):

        # Init inherit classes such as the WorkflowSpec
        super(WorkflowRunner, self).__init__(workflow=workflow, **kwargs)

        # Define task runner
        self.task_runner = None
        self.workflow_thread = None

        # Workflow state
        self._is_running = False
    
    def error_callback(self, failure, tid):
        """
        Process the output of a failed task and stage the next task to run
        if allowed

        :param failure: Twisted deferred error stack trace
        :type failure:  exception
        :param tid:     Task ID of failed task
        :type tid:      :py:int
        """

        task = self.get_task(tid)

        failure_message = ""
        if isinstance(failure, Exception) or isinstance(failure, str):
            failure_message = str(failure)
        elif isinstance(failure, Failure):
            failure_message = failure.value
        else:
            failure.getErrorMessage()

        logging.error('Task {0} ({1}) crashed with error: {2}'.format(task.nid, task.key, failure_message))

        # Update task meta data
        task.status = 'failed'
        task.task_metadata.endedAtTime.set()

        # Update workflow metadata
        metadata = self.workflow.query_nodes(key='project_metadata')
        metadata.update_time.set()
        self.is_running = False

        return

    def output_callback(self, output, tid):
        """
        Process the output of a task and stage the next task(s) to run.

        A successful task is expected to return some output. If None it
        is considered to have failed by the workflow manager.

        :param output: output of the task
        :type output:  :py:dict
        :param tid:    Task ID
        :type tid:     :py:int
        """

        # Get the task
        task = self.get_task(tid)

        # Update project metadata
        metadata = self.workflow.query_nodes(key='project_metadata')
        metadata.update_time.set()

        # Update task metadata
        # If the task returned no output at all, fail it
        if output is None:
            logging.error('Task {0} ({1}) returned no output'.format(task.nid, task.key))
            task.status = 'failed'
        else:
            # Update the task output data only if not already 'completed'
            if task.status not in ('completed', 'failed'):
                task.set_output(output)
                task.status = 'completed'
                task.task_metadata.endedAtTime.set()

            logging.info('Task {0} ({1}), status: {2}'.format(task.nid, task.key, task.status))

        # Switch workdir if needed
        if metadata.project_dir.get():
            os.chdir(metadata.project_dir.get())

        # If the task is completed, go to next
        next_task_nids = []
        if task.status == 'completed':
            
            # Get next task(s) to run
            next_task_nids.extend([ntask.nid for ntask in task.next_tasks()])
            logging.info('{0} new tasks to run with output of {1} ({2})'.format(len(next_task_nids), task.nid, task.key))

        # If the task failed, retry if allowed and reset status to "ready"
        if task.status == 'failed' and task.task_metadata.retry_count():
            task.task_metadata.retry_count.value -= 1
            task.status = 'ready'

            logging.warn('Task {0} ({1}) failed. Retry ({2} times left)'.format(task.nid, task.key,
                                                                                task.task_metadata.retry_count()))
            next_task_nids.append(task.nid)

        # If the active failed an no retry is allowed, save workflow and stop.
        if task.status == 'failed' and task.task_metadata.retry_count() == 0:
            logging.error('Task {0} ({1}) failed'.format(task.nid, task.key))
            if metadata.project_dir():
                self.save(os.path.join(metadata.project_dir(), 'workflow.jgf'))
            self.is_running = False
            return

        # If the task is completed but a breakpoint is defined, wait for the
        # breakpoint to be lifted
        if task.task_metadata.breakpoint():
            logging.info('Task {0} ({1}) finished but breakpoint is active'.format(task.nid, task.key))
            self.is_running = False
            return

        # No more new tasks
        if not next_task_nids:

            # Not finished but no active tasks anymore/breakpoint
            if not self.active_tasks and not self.is_completed:
                breakpoints = self.active_breakpoints
                if breakpoints:
                    logging.info('Active breakpoint: {0}'.format(', '.join([t.key for t in breakpoints])))
                self.is_running = False
                return

            # Finish of if there are no more tasks to run and all are completed
            if self.is_completed or self.has_failed:
                logging.info('finished workflow')
                self.is_running = False
                if not metadata.finish_time():
                    metadata.finish_time.set()
                if metadata.project_dir():
                    self.save(os.path.join(metadata.project_dir(), 'workflow.jgf'))
                return

        # Launch new tasks
        for tid in next_task_nids:
            self.run_task(tid)

    def run_task(self, tid):
        """
        Run a task by task ID (tid)

        Handles the setup procedure for running a task using a dedicated Task
        runner. The output or errors of a task are handled by the
        `output_callback` and `error_callback` methods respectively.

        Tasks to run are processed using the following rules:

        * If the task is currently active, stop and have the output callback
          function deal with it.
        * If the task has status 'ready' run it.
        * In all other cases, pass the task data to the output callback
          function. This is useful for hopping over finished tasks when
          relaunching a workflow for instance.

        :param tid: Task node identifier
        :type tid:  :py:int
        """

        # Get the task object from the graph. nid is expected to be in graph.
        # Check if the task has a 'run_task' method.
        task = self.get_task(tid)
        metadata = self.workflow.query_nodes(key='project_metadata')

        # Do not continue if the task is active
        if task.is_active:
            logging.debug('Task {0} ({1}) already active'.format(task.nid, task.key))
            return

        # Run the task if status is 'ready'
        if task.status == 'ready':
            logging.info('Task {0} ({1}), status: preparing'.format(task.nid, task.key))

            # Confirm again that the workflow is running
            self.is_running = True
            metadata.update_time.set()

            # Perform run preparations and run the task
            if task.prepare_run():
                task.run_task(self.output_callback, self.error_callback, task_runner=self.task_runner)
            else:
                self.error_callback('Task preparation failed', task.nid)

        # In all other cases, pass task data to default output callback
        # instructing it to not update the data but decide on the followup
        # workflow step to take.
        else:
            logging.info('Task {0} ({1}), status: {0}'.format(task.nid, task.key, task.status))
            self.output_callback({}, tid)

    @property
    def is_running(self):
        """
        Returns the global state of the workflow as running or not.

        :rtype: :py:bool
        """

        return self._is_running

    @is_running.setter
    def is_running(self, state):
        """
        Set the global state of the workflow as running or not.
        If the new state is 'False' first check if there are no other parallel
        active tasks.
        """

        if not state:
            state = len(self.active_tasks) >= 1
        self._is_running = state

    @property
    def is_completed(self):
        """
        Is the workflow completed successfully or not

        :rtype: :py:bool
        """

        return all([task.status in ('completed', 'disabled') for task in self.get_tasks()])

    @property
    def has_failed(self):
        """
        Did the workflow finish unsuccessfully?
        True if there are no more active tasks and at least one task has failed
        or was aborted
        """

        if not len(self.active_tasks) and any([task.status in ('failed', 'aborted') for task in self.get_tasks()]):
            return True

        return False

    @property
    def starttime(self):
        """
        Return the time stamp at which the workflow was last started

        :rtype: :py:int
        """

        metadata = self.workflow.query_nodes(key='project_metadata')
        return metadata.start_time.timestamp()

    @property
    def updatetime(self):
        """
        Return the time stamp at which the workflow was last updated

        :rtype: :py:int
        """

        metadata = self.workflow.query_nodes(key='project_metadata')
        return metadata.update_time.timestamp()

    @property
    def finishtime(self):
        """
        Return the time stamp at which the workflow finished or None
        if it has not yet finished

        :rtype: :py:int
        """

        if not self.is_running:
            metadata = self.workflow.query_nodes(key='project_metadata')
            return metadata.finish_time.timestamp()
        return None

    @property
    def runtime(self):
        """
        Return the total workflow runtime in seconds as the different between
        the start time and the finish time or last update time

        :rtype: :py:int
        """

        start = self.starttime or 0
        end = self.finishtime or self.updatetime

        # No update and finish time means the workflow was not started yet
        if not end:
            return 0

        return end - start

    @property
    def active_tasks(self):
        """
        Return all active tasks in the workflow

        :rtype: :py:list
        """

        return [task for task in self.get_tasks() if task.is_active]

    @property
    def failed_tasks(self):

        return [task for task in self.get_tasks() if task.status == 'failed']

    @property
    def active_breakpoints(self):
        """
        Return tasks with active breakpoint or None
        """

        return [task for task in self.get_tasks() if task.task_metadata.breakpoint.get(default=False)]

    def cancel(self):
        """
        Cancel the full workflow.

        This method will send a cancel request to all active tasks in the
        running workflow. Once there are no more active tasks the workflow
        run method will stop and the deamon thread will be closed.

        For canceling specific tasks please use the `cancel` function of the
        specific task retrieved using the `WorkflowSpec.get_task` method or
        workflow graph methods.
        """

        if not self.is_running:
            logging.info('Unable to cancel workflow that is not running.')
            return

        # Get active task
        active_tasks = self.active_tasks
        logging.info('Cancel tasks: {0}'.format(', '.join([t.key for t in active_tasks])))

        for task in active_tasks:
            task.cancel()

        metadata = self.workflow.query_nodes(key='project_metadata')
        metadata.update_time.set()

        self.is_running = False

    def get_task(self, tid=None, key=None):
        """
        Return a task by task ID (graph nid) or task name (key).

        :param tid:       nid of task to return
        :type tid:        :py:int
        """

        if tid:
            task = self.workflow.getnodes(tid)
        elif key:
            task = self.workflow.query_nodes(key=key)
        else:
            raise WorkflowError('Search on task ID (tid) or task name (key). None defined')

        if task.empty():
            raise WorkflowError('Task with tid {0} not in workflow'.format(tid))
        if not task.get('format') == 'task':
            raise WorkflowError('Node with tid {0} is no task object'.format(tid))

        return task

    def step_breakpoint(self, tid):
        """
        Continue a workflow at a task that is paused by a breakpoint

        :param tid: workflow task ID with active breakpoint
        :type tid:  :py:int
        """

        task = self.get_task(tid)
        if not task.task_metadata.breakpoint.get(default=False):
            logging.warn('No active breakpoint set on task {0}'.format(task.key))
            return

        # Remove the breakpoint
        task.task_metadata.breakpoint.set('value', False)
        logging.info('Remove breakpoint on task {0} ({1})'.format(tid, task.key))

    def input(self, tid, **kwargs):
        """
        Define task input and configuration data
        """

        task = self.get_task(tid)
        task.set_input(**kwargs)

    def output(self, tid=None):
        """
        Get workflow output
        Returns the output associated to all terminal tasks (leaf nodes) of
        the workflow or of any intermediate tasks identified by the task ID

        :param tid: task ID to return output for
        :type tid:  :py:int
        """

        task = self.get_task(tid)

        output = {}
        if task.status == 'completed':
            output = task.get_output()

        return output

    def run(self, project_dir="./mdstudio_workflow", tid=None, validate=True):
        """
        Run a workflow specification

        Runs the workflow until finished, failed or a breakpoint is reached.
        A workflow is a rooted Directed Acyclic Graph (DAG) that is started
        from the root node. It can be started from any node relative to the
        root as long as its parent(s) are successfully completed.

        The workflow will be executed on a different thread allowing for
        interactivity with the workflow instance while the workflow is
        running.

        By default, the workflow specification will be validated using the
        `validate` method of the WorkflowSpec class.

        :param tid:      Start the workflow from task ID
        :type tid:       :py:int
        :param validate: Validate the workflow before running it
        :type validate:  :py:bool
        """

        # Empty workflow, return
        if self.workflow.empty() or not len(self.workflow.query_nodes(format='task')):
            logging.info('Workflow contains no tasks')
            return

        # Start from workflow root by default
        tid = tid or self.workflow.root

        # Check if tid exists
        if tid not in self.workflow.nodes:
            raise WorkflowError('Task with tid {0} not in workflow'.format(tid))

        # Validate workflow before running?
        if validate:
            if not validate_workflow(self.workflow):
                return

        # Set is_running flag. Function as a thread-safe signal to indicate
        # that the workflow is running.
        project_metadata = self.workflow.query_nodes(key='project_metadata')
        if self.is_running:
            logging.warning('Workflow {0} is already running'.format(project_metadata.title()))
            return
        self.is_running = True

        # If there are steps that store results locally (store_output == True)
        # Create a project directory.
        if any(self.workflow.query_nodes(key="store_output").values()):
            project_metadata.project_dir.set('value', project_metadata.project_dir.get(default=project_dir))
            project_metadata.project_dir.makedirs()
        else:
            project_metadata.project_dir.set('value', None)

        logging.info('Running workflow: {0}, start task ID: {1}'.format(project_metadata.title(), tid))

        # Set workflow start time if not defined. Don't rerun to allow
        # continuation of unfinished workflow.
        if not project_metadata.start_time():
            project_metadata.start_time.set()

        # Spawn a thread
        self.workflow_thread = threading.Thread(target=self.run_task, args=[tid])
        self.workflow_thread.daemon = True
        self.workflow_thread.start()
