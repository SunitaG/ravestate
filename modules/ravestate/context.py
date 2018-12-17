# Ravestate context class

from reggol import get_logger
logger = get_logger(__name__)

from threading import Thread, Lock, Event
from typing import Optional, Any, Tuple, List, Set, Dict
from collections import defaultdict

from ravestate.icontext import IContext
from ravestate.module import Module
from ravestate.state import State
from ravestate.property import PropertyBase
from ravestate.activation import StateActivation
from ravestate import registry
from ravestate import argparse
from ravestate.config import Configuration
from ravestate.constraint import s, Signal
from ravestate.siginst import SignalInstance


class Context(IContext):

    default_signal_names: Tuple[str] = (":startup", ":shutdown", ":idle")
    default_property_signal_names: Tuple[str] = (":changed", ":pushed", ":popped", ":deleted")

    core_module_name = "core"
    import_modules_config = "import"
    tick_rate_config = "tickrate"

    _lock: Lock

    _properties: Dict[str, PropertyBase]
    _states: Set[State]
    _signals: Set[SignalInstance]
    _activations_per_signal_age: Dict[Signal, Dict[int, Set[State]]]

    _config: Configuration
    _core_config: Dict[str, Any]
    _run_task: Thread
    _shutdown_flag: Event

    def __init__(self, *arguments):
        """
        Construct a context from command line arguments.
        :param arguments: A series of command line arguments which can be parsed
         by the ravestate command line parser (see argparse.py).
        """
        modules, overrides, config_files = argparse.handle_args(*arguments)
        self._config = Configuration(config_files)
        self._core_config = {
            self.import_modules_config: [],
            self.tick_rate_config: 20
        }
        self._config.add_conf(Module(name=self.core_module_name, config=self._core_config))
        self._lock = Lock()
        self._shutdown_flag = Event()
        self._properties = dict()
        self._states = set()
        self._signals = set()
        self._activations_per_signal_age = Dict[Tuple[Signal, int], Set[State]]

        # Set required config overrides
        for module_name, key, value in overrides:
            self._config.set(module_name, key, value)

        # Load required modules
        for module_name in self.core_config[self.import_modules_config]+modules:
            self.add_module(module_name)

    def emit(self, signal: Signal, parents: Set[SignalInstance]=None, wipe: bool=False) -> None:
        """
        Emit a signal to the signal processing loop. Note:
         The signal will only be processed if run() has been called!
        :param signal: The signal to be emitted.
        """
        with self._lock:
            self._signals.add(signal)

    def run(self) -> None:
        """
        Creates a signal processing thread, starts it, and emits the :startup signal.
        """
        if self._run_task:
            logger.error("Attempt to start context twice!")
            return
        self._run_task = Thread(target=self._run_private)
        self._run_task.start()
        self.emit(s(":startup"))

    def shutting_down(self) -> bool:
        """
        Retrieve the shutdown flag value, which indicates whether shutdown() has been called.
        """
        return self._shutdown_flag.is_set()

    def shutdown(self) -> None:
        """
        Sets the shutdown flag and waits for the signal processing thread to join.
        """
        self._shutdown_flag.set()
        self.emit(s(":shutdown"))
        self._run_task.join()

    def add_module(self, module_name: str) -> None:
        """
        Add a module by python module folder name, or by ravestate module name.
        :param module_name: The name of the module to be added. If it is the
         name of a python module that has not been imported yet, the python module
         will be imported, and any ravestate modules registered during the python
         import will also be added to this context.
        """
        if registry.has_module(module_name):
            self._module_registration_callback(registry.get_module(module_name))
            return
        registry.import_module(module_name=module_name, callback=self._module_registration_callback)

    def add_state(self, *, st: State) -> None:
        """
        Add a state to this context. It will be indexed wrt/ the properties/signals
         it depends on. Error messages will be generated for unknown signals/properties.
        :param st: The state which should be added to this context.
        """
        if st in self._states:
            logger.error(f"Attempt to add state `{st.name}` twice!")
            return

        # make sure that all of the state's depended-upon properties exist
        for prop in st.read_props+st.write_props:
            if prop not in self._properties:
                logger.error(f"Attempt to add state which depends on unknown property `{prop}`!")

        # register the state's signal
        with self._lock:
            if st.signal:
                self._activations_per_signal_age[st.signal] = defaultdict(set)
            # check to recognize states using old signal implementation
            if isinstance(st.triggers, str):
                logger.error(f"Attempt to add state which depends on a signal `{st.triggers}`  "
                              f"defined as a String and not Signal.")

            # make sure that all of the state's depended-upon signals exist
            for signal in st.triggers.get_all_signals():
                if signal in self._activations_per_signal_age:
                    self._activations_per_signal_age[signal][0].add(st)
                else:
                    logger.error(f"Attempt to add state which depends on unknown signal `{signal}`!")
            self._states.add(st)

    def rm_state(self, *, st: State) -> None:
        """
        Remove a state from this context.
        :param st: The state to remove. An error message will be generated,
         if the state was not previously added to this context with add_state().
        """
        if st not in self._states:
            logger.error(f"Attempt to remove unknown state `{st.name}`!")
            return
        with self._lock:
            if st.signal:
                self._activations_per_signal_age.pop(st.signal)
            for signal in st.triggers.get_all_signals():
                    self._activations_per_signal_age[signal].remove(st)
            self._states.remove(st)

    def add_prop(self, *, prop: PropertyBase) -> None:
        """
        Add a property to this context. An error message will be generated, if a property with
         the same name has already been added previously.
        :param prop: The property object that should be added.
        """
        if prop.fullname() in self._properties.values():
            logger.error(f"Attempt to add property {prop.name} twice!")
            return
        # register property
        self._properties[prop.fullname()] = prop
        # register all of the property's signals
        with self._lock:
            for signalname in self.default_property_signal_names:
                self._activations_per_signal_age[s(prop.fullname() + signalname)] = defaultdict(set)

    def get_prop(self, key: str) -> Optional[PropertyBase]:
        """
        Retrieve a property object by that was previously added through add_prop()
         by it's full name. The full name is always the combination of the property's
         name and it's parent's name, joined with a colon: For example, if the name
         of a property is `foo` and it belongs to the module `bar` it's full name
         will be `bar:foo`.
        An error message will be generated if no property with the given name was
         added to the context, and None will be returned/
        :param key: The full name of the property.
        :return: The property object, or None, if no property with the given name
         was added to the context.
        """
        if key not in self._properties:
            logger.error(f"Attempt to retrieve unknown property by key `{key}`!")
            return None
        return self._properties[key]

    def conf(self, *, mod: str, key: Optional[str]=None) -> Any:
        """
        Get a single config value, or all config values for a particular module.
        :param mod: The module whose configuration should be retrieved.
        :param key: A specific config key of the given module, if only a single
         config value should be retrieved.
        :return: The value of a single config entry if key and module are both
         specified and valid, or a dictionary of config entries if only the
         module name is specified (and valid).
        """
        if key:
            return self.config.get(mod, key)
        return self.config.get_conf(mod)

    def _module_registration_callback(self, mod: Module):
        self.config.add_conf(mod)
        for prop in mod.props:
            self.add_prop(prop=prop)
        for st in mod.states:
            self.add_state(st=st)
        logger.info(f"Module {mod.name} added to session.")

    def _run_private(self):
        while not self._shutdown_flag:
            # Acquire new state activations for every signal instance
            # Update all state activations
            # Forget unreferenced signal instances
            # Increment age on active signal instances
            pass
