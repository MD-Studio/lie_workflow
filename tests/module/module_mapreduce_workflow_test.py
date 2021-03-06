# -*- coding: utf-8 -*-

"""
file: module_mapreduce_workflow_test.py

Unit tests construction and running the mapreduce workflow:

           6 -- 7
          /      \
    1 -- 2 -- 3 -- 4 -- 5
         \      /
          8 -- 9

Run it using the following scenarios:
- Duplicate output of 2 to 6, 3, and 8 and use a dedicated reducer Python
  function at 4 to process the combined output of 3, 7 and 9 when it comes
  available.
"""

import os
import time
import unittest

from tests.module.unittest_baseclass import UnittestPythonCompatibility

from mdstudio_workflow import Workflow, WorkflowSpec

currpath = os.path.abspath(os.path.dirname(__file__))
workflow_file_path = os.path.abspath(os.path.join(currpath, '../files/test-mapreduce-workflow.jgf'))


class BaseWorkflowRunnerTests(object):

    def test1_initial_workflow_status(self):
        """
        Workflow has not been run before.
        """

        self.assertFalse(self.wf.is_running)
        self.assertFalse(self.wf.is_completed)
        self.assertFalse(self.wf.has_failed)

    def test4_final_workflow_status(self):
        """
        Workflow should have been finished successfully
        """

        self.assertFalse(self.wf.is_running)
        self.assertTrue(self.wf.is_completed)
        self.assertFalse(self.wf.has_failed)

        self.assertIsNotNone(self.wf.starttime)
        self.assertIsNotNone(self.wf.finishtime)
        self.assertIsNotNone(self.wf.updatetime)
        self.assertTrue(6 < self.wf.runtime < 10)
        self.assertLessEqual(self.wf.updatetime, self.wf.finishtime)

    def test5_final_workflow_output(self):
        """
        Test the output of the python function calculation
        """

        result = {}
        for task in self.wf.get_tasks():
            o = task.get_output()
            result[task.key] = o.get('dummy')

        self.assertDictEqual(result, self.expected_output)


class TestBuildMapreduceWorkflow(UnittestPythonCompatibility):
    """
    Build the map-reduce workflow a shown in the file header using the default
    threader PythonTask runner
    """

    @classmethod
    def setUpClass(cls):
        """
        Setup up workflow spec class
        """

        cls.spec = WorkflowSpec()

    def test1_set_project_meta(self):
        """
        Set project meta data
        """

        metadata = self.spec.workflow.query_nodes(key='project_metadata')
        self.assertFalse(metadata.empty())

        metadata.title.set('value', 'Simple mapreduce workflow')
        metadata.description.set('value', 'Test a simple mapreduce workflow of 9 threaded python tasks')

        self.assertTrue(all([n is not None for n in [metadata.title(), metadata.description()]]))

    def test2_add_methods(self):
        """
        Test adding 10 blocking python tasks
        """

        for task in range(9):
            self.spec.add_task('test{0}'.format(task+1), custom_func="module.dummy_task_runners.task_runner",
                               store_output=False)

        self.assertEqual(len(self.spec), 9)

    def test3_add_connections(self):
        """
        Test connecting 9 tasks in a branched fashion
        """

        edges = (('test1', 'test2'), ('test2', 'test3'), ('test3', 'test4'), ('test4', 'test5'), ('test2', 'test6'),
                 ('test6', 'test7'), ('test7', 'test4'), ('test2', 'test8'), ('test8', 'test9'), ('test9', 'test4'))
        tasks = dict([(t.key, t.nid) for t in self.spec.get_tasks()])
        for edge in edges:
            self.spec.connect_task(tasks[edge[0]], tasks[edge[1]])

        self.assertTrue(len(self.spec.workflow.adjacency[tasks['test2']]), 4)
        self.assertTrue(len(self.spec.workflow.adjacency[tasks['test4']]), 4)

    def test4_save_workflow(self):
        """
        Test save workflow to default jgf format
        """

        self.spec.save(path=workflow_file_path)
        self.assertTrue(os.path.exists(workflow_file_path))


class TestRunMapreduceWorkflowDefault(BaseWorkflowRunnerTests, UnittestPythonCompatibility):
    """
    Run the branched workflow build in TestBuildBranchedWorkflow
    """

    expected_output = {u'test7': 9, u'test1': 5, u'test8': 7, u'test9': 10, u'test2': 6, u'test3': 8,
                       u'test4': 27, u'test5': 28, u'test6': 8}

    @classmethod
    def setUpClass(cls):
        """
        Load previously created linear workflow spec file
        """

        if not os.path.exists(workflow_file_path):
            raise unittest.SkipTest('TestBuildBranchedWorkflow failed to build workflow')

        cls.wf = Workflow()
        cls.wf.load(workflow_file_path)

    def test2_define_input(self):
        """
        Set initial input to the workflow but change the custom Python function
        of task 4 to a dedicated reducer function.
        """

        self.wf.input(self.wf.workflow.root, dummy=3)
        sleep_times = [1, 2, 1, 3, 1, 2, 1, 1, 2]
        for i, task in enumerate(self.wf.get_tasks()):
            task.set_input(add_number=sleep_times[i], sleep=sleep_times[i])

            if task.key == 'test4':
                task.custom_func.value = 'module.dummy_task_runners.reduce_function'

    def test3_run_workflow(self):
        """
        Test running the workflow
        """

        # Run the workflow
        self.wf.run()

        # Blocking: wait until workflow is no longer running
        while self.wf.is_running:
            time.sleep(1)


class TestZcleanup(UnittestPythonCompatibility):

    @classmethod
    def setUpClass(cls):
        """
        Cleanup workflow files created by other tests
        """

        if os.path.exists(workflow_file_path):
            os.remove(workflow_file_path)

    def test_dummy(self):

        pass
