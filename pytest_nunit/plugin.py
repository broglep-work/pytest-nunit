"""
Nunit plugin for pytest

Based (loosely) on the Junit XML output.

Shares the same pattern of CLI options for ease of use.
"""
from _pytest.config import filename_arg

import os
from datetime import datetime
import functools

from .nunit import NunitTestRun

import logging

logging.basicConfig()
log = logging.getLogger("__name__")


def pytest_addoption(parser):
    group = parser.getgroup("terminal reporting")
    group.addoption(
        "--nunitxml",
        "--nunit-xml",
        action="store",
        dest="nunit_xmlpath",
        metavar="path",
        type=functools.partial(filename_arg, optname="--nunitxml"),
        default=None,
        help="create nunit-xml style report file at given path.",
    )
    group.addoption(
        "--nunitprefix",
        "--nunit-prefix",
        action="store",
        metavar="str",
        default=None,
        help="prepend prefix to classnames in nunit-xml output",
    )
    parser.addini(
        "nunit_suite_name", "Test suite name for NUnit report", default="pytest"
    )
    parser.addini(
        "nunit_logging",
        "Write captured log messages to NUnit report: "
        "one of no|system-out|system-err",
        default="no",
    )  # choices=['no', 'stdout', 'stderr'])
    parser.addini(
        "nunit_log_passing_tests",
        "Capture log information for passing tests to NUnit report: ",
        type="bool",
        default=True,
    )
    parser.addini(
        "nunit_duration_report",
        "Duration time to report: one of total|call",
        default="total",
    )  # choices=['total', 'call'])


def pytest_configure(config):
    nunit_xmlpath = config.option.nunit_xmlpath
    # prevent opening xmllog on slave nodes (xdist)
    if nunit_xmlpath and not hasattr(config, "slaveinput"):
        config._nunitxml = NunitXML(
            nunit_xmlpath,
            config.option.nunitprefix,
            config.getini("nunit_suite_name"),
            config.getini("nunit_logging"),
            config.getini("nunit_duration_report"),
            config.getini("nunit_log_passing_tests"),
        )
        config.pluginmanager.register(config._nunitxml)


def pytest_unconfigure(config):
    nunitxml = getattr(config, "_nunitxml", None)
    if nunitxml:
        del config._nunitxml
        config.pluginmanager.unregister(nunitxml)


class _NunitNodeReporter:
    def __init__(self, nodeid, nunit_xml):
        self.id = nodeid
        self.nunit_xml = nunit_xml
        self.duration = 0.0

    def append(self, node):
        self.nunit_xml.add_stats(type(node).__name__)
        self.nodes.append(node)

    def record_testreport(self, testreport):
        log.debug("record_test_report:{0}".format(testreport))
        if testreport.when == "setup":
            r = self.nunit_xml.cases[testreport.nodeid] = {
                "setup-report": testreport,
                "call-report": None,
                "teardown-report": None,
                "idref": self.nunit_xml.idrefindex,
            }
            self.nunit_xml.idrefindex += 1  # Inc. node id ref counter
            r["start"] = datetime.utcnow()  # Will be overridden if called
        elif testreport.when == "call":
            r = self.nunit_xml.cases[testreport.nodeid]
            r["start"] = datetime.utcnow()
            r["call-report"] = testreport
            # TODO : Extra data
        elif testreport.when == "teardown":
            r = self.nunit_xml.cases[testreport.nodeid]
            r["stop"] = datetime.utcnow()
            r["duration"] = (
                (r["stop"] - r["start"]).total_seconds() if r["call-report"] else 0
            )  # skipped.
            r["teardown-report"] = testreport

            if r["setup-report"].outcome == "skipped":
                r["outcome"] = "skipped"
            elif "failed" in [
                r["setup-report"].outcome,
                r["call-report"].outcome,
                testreport.outcome,
            ]:
                r["outcome"] = "failed"
            else:
                r["outcome"] = "passed"

    def finalize(self):
        log.debug("finalize")


class NunitXML:
    def __init__(
        self,
        logfile,
        prefix,
        suite_name="pytest",
        logging="no",
        report_duration="total",
        log_passing_tests=True,
    ):
        logfile = os.path.expanduser(os.path.expandvars(logfile))
        self.logfile = os.path.normpath(os.path.abspath(logfile))
        self.prefix = prefix
        self.suite_name = suite_name
        self.logging = logging
        self.log_passing_tests = log_passing_tests
        self.report_duration = report_duration
        self.stats = dict.fromkeys(
            ["error", "passed", "failure", "skipped", "total", "asserts"], 0
        )
        self.node_reporters = {}  # nodeid -> _NodeReporter
        self.node_reporters_ordered = []
        self.cases = dict()

        self.idrefindex = 100  # Create a unique ID counter

    def finalize(self, report):
        nodeid = getattr(report, "nodeid", report)
        # local hack to handle xdist report order
        slavenode = getattr(report, "node", None)
        reporter = self.node_reporters.pop((nodeid, slavenode))
        if reporter is not None:
            reporter.finalize()

    def node_reporter(self, report):
        nodeid = getattr(report, "nodeid", report)
        # local hack to handle xdist report order
        slavenode = getattr(report, "node", None)

        key = nodeid, slavenode

        if key in self.node_reporters:
            # TODO: breaks for --dist=each
            return self.node_reporters[key]

        reporter = _NunitNodeReporter(nodeid, self)

        self.node_reporters[key] = reporter
        self.node_reporters_ordered.append(reporter)

        return reporter

    def _opentestcase(self, report):
        reporter = self.node_reporter(report)
        reporter.record_testreport(report)
        return reporter

    def pytest_runtest_logreport(self, report):
        reporter = self._opentestcase(report)

    def update_testcase_duration(self, report):
        """accumulates total duration for nodeid from given report and updates
        the Junit.testcase with the new total if already created.
        """
        if self.report_duration == "total" or report.when == self.report_duration:
            reporter = self.node_reporter(report)
            reporter.duration += getattr(report, "duration", 0.0)

    def pytest_collectreport(self, report):
        pass

    def pytest_internalerror(self, excrepr):
        reporter = self.node_reporter("internal")

    def pytest_sessionstart(self, session):
        self.suite_start_time = datetime.utcnow()

    def pytest_sessionfinish(self, session, exitstatus):
        # Build output file
        dirname = os.path.dirname(os.path.abspath(self.logfile))
        if not os.path.isdir(dirname):
            os.makedirs(dirname)
        self.suite_stop_time = datetime.utcnow()
        self.suite_time_delta = (
            self.suite_stop_time - self.suite_start_time
        ).total_seconds()

        self.stats["total"] = session.testscollected
        self.stats["passed"] = len(
            list(case for case in self.cases.values() if case["outcome"] == "passed")
        )
        self.stats["failure"] = len(
            list(case for case in self.cases.values() if case["outcome"] == "failed")
        )
        self.stats["skipped"] = len(
            list(case for case in self.cases.values() if case["outcome"] == "skipped")
        )

        with open(self.logfile, "w", encoding="utf-8") as logfile:
            logfile.write('<?xml version="1.0" encoding="utf-8"?>')
            result = NunitTestRun(self).generate_xml()
            logfile.write(result)

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_sep("-", "generated Nunit xml file: %s" % (self.logfile))
