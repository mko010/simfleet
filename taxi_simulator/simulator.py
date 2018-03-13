import logging

import pandas as pd
from tabulate import tabulate
import thread
import os
import json
import pickle

from multiprocessing import Process
from multiprocessing.queues import SimpleQueue

from spade import spade_backend
from xmppd.xmppd import Server
from flask import Flask

from utils import crossdomain
from scenario import Scenario
from coordinator import CoordinatorAgent
from route import RouteAgent

logger = logging.getLogger()


class SimulationConfig(object):
    """
    Dataclass to store the :class:`Simulator` config
    """
    def __init__(self):
        self.simulation_name = None
        self.max_time = None
        self.taxi_strategy = None
        self.passenger_strategy = None
        self.coordinator_strategy = None
        self.scenario = None
        self.num_taxis = None
        self.num_passengers = None
        self.http_port = None
        self.coordinator_name = None
        self.coordinator_password = None
        self.backend_port = None
        self.verbose = None


class Simulator(object):
    """
    The Simulator. It manages all the simulation processes.
    Tasks done by the simulator at initialization:
        #. Create the XMPP server
        #. Run the SPADE backend
        #. Run the coordinator and route agents.
        #. Create agents passed as parameters (if any).
        #. Create agents defined in scenario (if any).
        #. Create a backend web server (:class:`FlaskBackend`) to listen for async commands.

    After these tasks are done in the Simulator constructor, the simulation is started when the :func:`run` method is called.
    """
    def __init__(self, config):
        self.config = config
        self.pretty_name = "({})".format(self.config.simulation_name) if self.config.simulation_name else ""
        self.verbose = self.config.verbose

        self.host = "127.0.0.1"

        self.simulation_time = 0

        self.coordinator_agent = None

        self.command_queue = SimpleQueue()

        self.df_avg = None
        self.passenger_df = None
        self.taxi_df = None

        # generate config
        if not os.path.exists("spade.xml") or not os.path.exists("xmppd.xml"):
            os.system("configure.py 127.0.0.1")

        # reset user_db
        with open("user_db.xml", 'w') as f:
            pickle.dump({"127.0.0.1": {}}, f)

        debug_level = ['always'] if self.config.verbose > 2 else []
        self.xmpp_server = Server(cfgfile="xmppd.xml", cmd_options={'enable_debug': debug_level,
                                                                    'enable_psyco': False})

        logger.info("Starting Taxi Simulator {}".format(self.pretty_name))
        thread.start_new_thread(self.xmpp_server.run, tuple())
        logger.debug("XMPP server running.")
        self.platform = spade_backend.SpadeBackend(self.xmpp_server, "spade.xml")
        self.platform.start()
        logger.debug("Running SPADE platform.")

        debug_level = ['always'] if self.verbose > 1 else []
        self.coordinator_agent = CoordinatorAgent("{}@{}".format(config.coordinator_name, self.host),
                                                  password=config.coordinator_password,
                                                  debug=debug_level,
                                                  http_port=config.http_port,
                                                  backend_port=config.backend_port,
                                                  debug_level=debug_level)
        self.coordinator_agent.set_strategies(config.coordinator_strategy,
                                              config.taxi_strategy,
                                              config.passenger_strategy)
        self.coordinator_agent.start()

        self.route_agent = RouteAgent("route@{}".format(self.host), "r0utepassw0rd", debug_level)
        self.route_agent.start()

        logger.info("Creating {} taxis and {} passengers.".format(config.num_taxis, config.num_passengers))
        Scenario.create_agents_batch("taxi", config.num_taxis, self.coordinator_agent)
        Scenario.create_agents_batch("passenger", config.num_passengers, self.coordinator_agent)

        if config.scenario:
            scenario = Scenario(config.scenario, debug_level)
            for agent in scenario.taxis:
                self.coordinator_agent.add_taxi(agent)
                agent.start()
            for agent in scenario.passengers:
                self.coordinator_agent.add_passenger(agent)
                agent.start()

        self.web_backend_process = Process(None, _worker, "async web interface listener",
                                           [self.command_queue, "127.0.0.1",
                                            config.backend_port, False])
        self.web_backend_process.start()

    def is_simulation_finished(self):
        """
        Checks if the simulation is finished.
        A simulation is finished if the max simulation time has been reached or when the coordinator says it.

        Returns:
            bool: whether the simulation is finished or not.
        """
        if self.config.max_time is None:
            return False
        return self.time_is_out() or self.coordinator_agent.is_simulation_finished()

    def time_is_out(self):
        """
        Checks if the max simulation time has been reached.

        Returns:
            bool: whether the max simulation time has been reached or not.
        """
        return self.coordinator_agent.get_simulation_time() > self.config.max_time

    def run(self):
        """
        Starts the simulation (tells the coordinator agent to start the simulation).
        """
        self.coordinator_agent.run_simulation()

    def process_queue(self):
        """
        Queries the command queue if there is any command to execute. If true, runs the command.

        At the moment the only command available is to create new taxis and passengers.
        """
        if not self.command_queue.empty():
            ntaxis, npassengers = self.command_queue.get()
            logger.info("Creating {} taxis and {} passengers.".format(ntaxis, npassengers))
            Scenario.create_agents_batch("taxi", ntaxis, self.coordinator_agent)
            Scenario.create_agents_batch("passenger", npassengers, self.coordinator_agent)

    def stop(self):
        """
        Finishes the simulation and prints simulation stats.
        Tasks done when a simulation is stopped:
            #. Terminate web backend.
            #. Stop participant agents.
            #. Print stats.
            #. Stop Route agent.
            #. Stop Coordinator agent.
            #. Shutdown SPADE backend.
            #. Shutdown XMPP server.
        """
        self.simulation_time = self.coordinator_agent.get_simulation_time()

        logger.info("\nTerminating... ({0:.1f} seconds elapsed)".format(self.simulation_time))

        self.web_backend_process.terminate()

        self.coordinator_agent.stop_agents()

        self.print_stats()

        self.route_agent.stop()
        self.coordinator_agent.stop()
        self.platform.shutdown()
        self.xmpp_server.shutdown("")

    def collect_stats(self):
        """
        Collects stats from all participant agents and from the simulation and stores it in three dataframes.
        """
        passenger_df = self.coordinator_agent.get_passenger_stats()
        self.passenger_df = passenger_df[["name", "waiting_time", "total_time", "status"]]
        taxi_df = self.coordinator_agent.get_taxi_stats()
        self.taxi_df = taxi_df[["name", "assignments", "distance", "status"]]
        stats = self.coordinator_agent.get_stats()
        df_avg = pd.DataFrame.from_dict({"Avg Waiting Time": [stats["waiting"]],
                                         "Avg Total Time": [stats["totaltime"]],
                                         "Simulation Finished": [stats["finished"]],
                                         "Simulation Time": [self.simulation_time]
                                         })
        columns = []
        if self.config.simulation_name:
            df_avg["Simulation Name"] = self.config.simulation_name
            columns = ["Simulation Name"]
        columns += ["Avg Waiting Time", "Avg Total Time", "Simulation Time"]
        if self.config.max_time:
            df_avg["Max Time"] = self.config.max_time
            columns += ["Max Time"]
        columns += ["Simulation Finished"]
        self.df_avg = df_avg[columns]

    def get_stats(self):
        """
        Returns the dataframes collected by :func:`collect_stats`

        Returns:
            :obj:`pandas.DataFrame`, :obj:`pandas.DataFrame`, :obj:`pandas.DataFrame`: average df, passengers df and taxi df
        """
        return self.df_avg, self.passenger_df, self.taxi_df

    def print_stats(self):
        """
        Prints the dataframes collected by :func:`collect_stats`.
        """
        if self.df_avg is None:
            self.collect_stats()

        print("Simulation Results")
        print(tabulate(self.df_avg, headers="keys", showindex=False, tablefmt="fancy_grid"))
        print("Passenger stats")
        print(tabulate(self.passenger_df, headers="keys", showindex=False, tablefmt="fancy_grid"))
        print("Taxi stats")
        print(tabulate(self.taxi_df, headers="keys", showindex=False, tablefmt="fancy_grid"))

    def write_file(self, filename, fileformat="json"):
        """
        Writes the dataframes collected by :func:`collect_stats` in JSON or Excel format.

        Args:
            filename (str): name of the output file to be written.
            fileformat (str): format of the output file. Choices: json or excel
        """
        if self.df_avg is None:
            self.collect_stats()
        if fileformat == "json":
            self.write_json(filename)
        elif fileformat == "excel":
            self.write_excel(filename)

    def write_json(self, filename):
        """
        Writes the collected data by :func:`collect_stats` in a json file.

        Args:
            filename (str): name of the json file.
        """
        data = {
            "simulation": json.loads(self.df_avg.to_json(orient="index"))["0"],
            "passengers": json.loads(self.passenger_df.to_json(orient="index")),
            "taxis": json.loads(self.taxi_df.to_json(orient="index"))
        }

        with open(filename, 'w') as f:
            f.seek(0)
            json.dump(data, f, indent=4)

    def write_excel(self, filename):
        """
        Writes the collected data by :func:`collect_stats` in an excel file.

        Args:
            filename (str): name of the excel file.
        """
        writer = pd.ExcelWriter(filename)
        self.df_avg.to_excel(writer, 'Simulation')
        self.passenger_df.to_excel(writer, 'Passengers')
        self.taxi_df.to_excel(writer, 'Taxis')
        writer.save()


def _worker(command_queue, host, port, debug):
    FlaskBackend(command_queue, host, port, debug)


class FlaskBackend(object):
    def __init__(self, command_queue, host='0.0.0.0', port=5000, debug=False):
        self.command_queue = command_queue
        self.app = Flask('FlaskBackend')
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        self.app.add_url_rule("/generate/taxis/<int:ntaxis>/passengers/<int:npassengers>", "generate", self.generate)
        self.app.run(host=host, port=port, debug=debug, use_reloader=debug)

    @crossdomain(origin='*')
    def generate(self, ntaxis, npassengers):
        self.command_queue.put((ntaxis, npassengers))
        return ""
