# BSD 3-Clause License
#
# Copyright (c) 2023, BISDN GmbH
# All rights reserved.

#!/usr/bin/env python3

"""session context manager module"""

# pylint: disable=no-member
# pylint: disable=too-many-locals
# pylint: disable=too-many-nested-blocks
# pylint: disable=too-many-statements
# pylint: disable=too-many-branches

import os
import sys
import enum
import time
import logging
import argparse
import traceback
import hashlib
import socket
import pathlib
import contextlib
import threading
import yaml

from upsf_client.upsf import (
    UPSF,
    UpsfError,
)


class DerivedState(enum.Enum):
    """DerivedState"""

    UNKNOWN = 0
    INACTIVE = 1
    ACTIVE = 2
    UPDATING = 3
    DELETING = 4
    DELETED = 5


def str2bool(value):
    """map string to boolean value"""
    return value.lower() in [
        "true",
        "1",
        "t",
        "y",
        "yes",
    ]


class SessionContextManager(threading.Thread):
    """class SessionContextManager"""

    _defaults = {
        # upsf host, default: 127.0.0.1
        "upsf_host": os.environ.get("UPSF_HOST", "127.0.0.1"),
        # upsf port, default: 50051
        "upsf_port": os.environ.get("UPSF_PORT", 50051),
        # configuration file, default: /etc/upsf/policy.yaml
        "config_file": os.environ.get("CONFIG_FILE", "/etc/upsf/policy.yaml"),
        # default shard name, default: default-shard
        "default_shard_name": os.environ.get("DEFAULT_SHARD_NAME", "default-shard"),
        # default required quality, default: 1000
        "default_required_quality": os.environ.get("DEFAULT_REQUIRED_QUALITY", 1000),
        # default required service groups, default: basic-internet (comma separated string)
        "default_required_service_groups": os.environ.get(
            "DEFAULT_REQUIRED_SERVICE_GROUPS", "basic-internet"
        ),
        # periodic background thread: time interval
        "registration_interval": os.environ.get("REGISTRATION_INTERVAL", 60),
        # register shards periodically
        "upsf_auto_register": os.environ.get("UPSF_AUTO_REGISTER", "yes"),
        # loglevel, default: 'info'
        "loglevel": os.environ.get("LOGLEVEL", "info"),
    }

    _loglevels = {
        "critical": logging.CRITICAL,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "error": logging.ERROR,
        "debug": logging.DEBUG,
    }

    def __init__(self, **kwargs):
        """__init__"""
        threading.Thread.__init__(self)

        self._stop_thread = threading.Event()

        self.initialize(**kwargs)

    def initialize(self, **kwargs):
        """initialize"""

        for key, value in self._defaults.items():
            setattr(self, key, kwargs.get(key, value))

        # logger
        self._log = logging.getLogger(__name__)
        self._log.setLevel(self._loglevels[self.loglevel])

        # upsf instance
        self._upsf = UPSF(
            upsf_host=self.upsf_host,
            upsf_port=self.upsf_port,
        )

        # create default session contexts
        self.create_default_items()
        self._upsf_auto_register = None
        # create predefined shards
        if str2bool(self.upsf_auto_register):
            self._upsf_auto_register = threading.Thread(
                target=SessionContextManager.upsf_register_task,
                kwargs={
                    "entity": self,
                    "interval": self.registration_interval,
                },
                daemon=True,
            )
            self._upsf_auto_register.start()

        # initial mapping
        self.map_all_sessions()

    def __str__(self):
        """return simple string"""
        return f"{self.__class__.__name__}()"

    def __repr__(self):
        """return descriptive string"""
        _attributes = "".join(
            [
                f"{key}={getattr(self, key, None)}, "
                for key, value in self._defaults.items()
            ]
        )
        return f"{self.__class__.__name__}({_attributes})"

    @property
    def log(self):
        """return read-only logger"""
        return self._log

    @staticmethod
    def session_hash(**kwargs):
        """hashify session context to uniquely identify a session"""
        return hashlib.md5(  # nosec B303
            kwargs.get("circuit_id", "").encode()
            + kwargs.get("remote_id", "").encode()
            + kwargs.get("source_mac_address", "").encode()
            + str(kwargs.get("svlan", "")).encode()
            + str(kwargs.get("cvlan", "")).encode()
        ).hexdigest()

    def context_dump(self):
        """dump all session contexts"""
        for _, context in self._session_contexts.items():
            self.log.info(
                {
                    "entity": str(self),
                    "event": "context_dump",
                    "context.name": context.name,
                    "context.metadata.derived_state": DerivedState(
                        context.metadata.derived_state
                    ).name.lower(),
                    "context.spec.filter.lladdr": context.spec.session_filter.source_mac_address,
                    "context.spec.filter.s_tag": context.spec.session_filter.svlan,
                    "context.spec.filter.c_tag": context.spec.session_filter.cvlan,
                    "context.spec.circuit_id": context.spec.circuit_id,
                    "context.spec.remote_id": context.spec.remote_id,
                    "context.spec.desired_state.shard": str(
                        context.spec.desired_state.shard
                    ),
                    "context.status.user_plane_shard": str(
                        context.status.current_state.user_plane_shard
                    ),
                    "context.status.tsf_shard": str(
                        context.status.current_state.tsf_shard
                    ),
                }
            )

    def shard_default(self):
        """get default shard"""
        for shard_id, shard in self._shards.items():
            if shard.name != self.default_shard_name:
                continue
            return shard_id
        return None

    def map_all_sessions(self):
        """map all existing session context to shards"""

        for sctx in self._upsf.list_session_contexts():
            self.map_session(sctx)

    def map_session(self, sctx):
        """assign a shard to a session context without desired or invalid shard"""

        try:
            params = {}

            shards = self._upsf.list_shards()

            # number of shards available
            n_shards = len(shards)

            # sanity check: no shards available => nothing to do
            if not n_shards:
                self.log.warning(
                    {
                        "entity": str(self),
                        "event": "map_session: no shards available",
                    }
                )
                return

            # service gateway user planes
            sgups = self._upsf.list_service_gateway_user_planes()
            n_sgups = len(sgups)

            # sanity check: no sgups available => nothing to do
            if not n_sgups:
                self.log.warning(
                    {
                        "entity": str(self),
                        "event": "map_session: no service gateway user planes available",
                    }
                )
                return

            # set required quality
            if sctx.spec.required_quality in (
                0,
                None,
                "",
            ):
                params["required_quality"] = int(self.default_required_quality)

            # set required service groups
            if sctx.spec.required_service_group in (
                [],
                [""],
                None,
                "",
            ):
                params["required_service_group"] = [
                    rsg
                    for rsg in self.default_required_service_groups.split(",")
                    if rsg
                    not in (
                        None,
                        "",
                    )
                ]

            # set shard
            if sctx.spec.desired_state.shard in (
                None,
                "",
            ) and sctx.spec.required_service_group not in (
                [],
                [""],
                None,
                "",
            ):
                # potential shard candidates for selected sgup
                shard_candidates = {shard.name: shard for shard in shards}

                # set of service gateway user planes for shard candidates
                shard_candidate_sgups = set(
                    shard.spec.desired_state.service_gateway_user_plane
                    for shard_name, shard in shard_candidates.items()
                )

                # create dict: key=sgup.name, value=sgup
                service_gateway_user_planes = {
                    sgup.name: sgup
                    for sgup in sgups
                    if sgup.name in shard_candidate_sgups
                }

                # filter sgup candidates based on load and supported service groups
                sgup_candidates = set(
                    sgup_name
                    for sgup_name, sgup in service_gateway_user_planes.items()
                    if set(sgup.spec.supported_service_group).issuperset(
                        set(sctx.spec.required_service_group)
                    )
                    and sgup.status.allocated_session_count
                    < sgup.spec.max_session_count
                )

                # any sgup candidates available at all?
                if len(sgup_candidates) == 0:
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "no sgup candidates available for session context, ignoring",
                            "sctx.name": sctx.name,
                            "sctx.required_service_group": set(
                                sctx.spec.required_service_group
                            ),
                        }
                    )
                    return

                # acquire dict with sgup load values
                sgup_load = {
                    sgup_name: float(
                        service_gateway_user_planes[
                            sgup_name
                        ].status.allocated_session_count
                        / service_gateway_user_planes[sgup_name].spec.max_session_count
                    )
                    for sgup_name in sgup_candidates
                    if sgup_name in service_gateway_user_planes
                    and service_gateway_user_planes[sgup_name].spec.max_session_count
                    > 0
                    and service_gateway_user_planes[
                        sgup_name
                    ].status.allocated_session_count
                    < service_gateway_user_planes[sgup_name].spec.max_session_count
                }

                # get least loaded sgup from candidates
                sgup_name_selected = min(sgup_load, key=sgup_load.get)

                # potential shard candidates for selected sgup
                shard_candidates = {
                    shard.name: shard
                    for shard in shards
                    if shard.spec.desired_state.service_gateway_user_plane
                    == sgup_name_selected
                }

                # any shard candidates available at all?
                if len(shard_candidates) == 0:
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "no shard candidates available for session context, ignoring",
                            "sctx.name": sctx.name,
                        }
                    )
                    return

                # acquire dict with shard load values
                shard_load = {
                    shard_name: float(
                        shard.status.allocated_session_count
                        / shard.spec.max_session_count
                    )
                    for shard_name, shard in shard_candidates.items()
                    if shard.spec.max_session_count > 0
                    and shard.status.allocated_session_count
                    < shard.spec.max_session_count
                }

                # get least loaded shard from candidates
                shard_name_selected = min(shard_load, key=shard_load.get)

                # selected sgup
                params["desired_shard"] = shard_name_selected

                self.log.debug(
                    {
                        "entity": str(self),
                        "marker": "MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM",
                        "event": "selected service gateway user plane and shard",
                        "sctx.name": sctx.name,
                        "sctx.rsgs": list(sctx.spec.required_service_group),
                        "sgup_candidates": sgup_candidates,
                        "sgup_load": sgup_load,
                        "sgup_selected": sgup_name_selected,
                        "shard_candidates": shard_candidates.keys(),
                        "shard_load": shard_load,
                        "shard_selected": shard_name_selected,
                    }
                )

                shard_params = {
                    "name": shard_name_selected,
                    "allocated_session_count": self._upsf.get_shard(
                        name=shard_name_selected
                    ).status.allocated_session_count
                    + 1,
                }

                self._upsf.update_shard(
                    **shard_params,
                )

                # get SGUP from UPSF to avoid race condition
                sgup_selected = self._upsf.get_service_gateway_user_plane(name=sgup_name_selected)

                sgup_params = {
                    "name": sgup_name_selected,
                    "allocated_session_count": sgup_selected.status.allocated_session_count
                    + 1,
                }

                self._upsf.update_service_gateway_user_plane(
                    **sgup_params,
                )

            # update session context if any attribute has changed
            if len(params):
                params["name"] = sctx.name
                self.log.info(
                    {
                        "entity": str(self),
                        "event": "updating session context",
                        "sctx.name": sctx.name,
                        "params": params,
                    }
                )

                self._upsf.update_session_context(
                    **params,
                )

        except UpsfError as error:
            self.log.error(
                {
                    "entity": str(self),
                    "event": "error occurred",
                    "error": error,
                    "traceback": traceback.format_exc(),
                }
            )

    def create_session_context(self, entry, shards):
        """create session context"""
        sctxs = {sctx.name: sctx for sctx in self._upsf.list_session_contexts()}

        # get hash for identifying session
        sctx_hash = SessionContextManager.session_hash(
            circuit_id=entry.get("circuitId", ""),
            remote_id=entry.get("remoteId", ""),
            source_mac_address=entry.get("sourceMacAddress", ""),
            svlan=entry.get("svlan", "0"),
            cvlan=entry.get("cvlan", "0"),
        )

        # ignore existing session contexts
        if sctx_hash in sctxs:
            self.log.warning(
                {
                    "entity": str(self),
                    "event": "session context exists already",
                    "sctx.hash": sctx_hash,
                    "entry": entry,
                }
            )
            return None

        # description
        desc_list = [
            f"name={entry.get('name', '')}",
            f"customer_type={entry.get('customerType', 'residential')}",
        ]

        # # description: circuitId
        # if entry.get("circuitId", None) not in (
        #     "",
        #     None,
        # ):
        #     desc_list.append(f"circuit_id={entry.get('circuitId', '')}")

        # # description: remoteId
        # if entry.get("remoteId", None) not in (
        #     "",
        #     None,
        # ):
        #     desc_list.append(f"remote_id={entry.get('remoteId', '')}")

        # # description: sourceMacAddress
        # if entry.get("sourceMacAddress", None) not in (
        #     "",
        #     None,
        # ):
        #     desc_list.append(f"source_mac_address={entry.get('sourceMacAddress')}")

        # # description: svlan
        # if entry.get("svlan", None) not in (
        #     "0",
        #     None,
        # ):
        #     desc_list.append(f"svlan={entry.get('svlan', '')}")

        # # description: cvlan
        # if entry.get("cvlan", None) not in (
        #     "0",
        #     None,
        # ):
        #     desc_list.append(f"cvlan={entry.get('cvlan', '')}")

        # session context parameters
        params = {
            "name": sctx_hash,
            "required_service_group": entry.get(
                "requiredServiceGroups",
                self.default_required_service_groups.split(","),
            ),
            "required_quality": entry.get(
                "requiredQuality", self.default_required_quality
            ),
            "description": ";".join(desc_list),
            "circuit_id": entry.get("circuitId", ""),
            "remote_id": entry.get("remoteId", ""),
            "source_mac_address": entry.get("sourceMacAddress", ""),
            "svlan": int(entry.get("svlan", "0")),
            "cvlan": int(entry.get("cvlan", "0")),
        }

        # specific shard requested?
        if entry.get("shard", None) not in (
            "",
            None,
        ):
            # sanity check: shard must exist, delay sctx creation otherwise
            if entry["shard"] not in shards:
                self.log.warning(
                    {
                        "entity": str(self),
                        "event": "desired shard for session context "
                        "not found, ignoring",
                        "sctx.hash": sctx_hash,
                        "sctx.name": entry["name"],
                        "shard.name": entry["shard"],
                    }
                )
                return None

            # add key to dict params
            params["desired_shard"] = entry["shard"]

        self.log.info(
            {
                "entity": str(self),
                "event": "add entry",
                "entry": entry,
                "params": params,
            }
        )

        # create new session context
        return self._upsf.create_session_context(**params).session_context

    @staticmethod
    def upsf_register_task(**kwargs):
        """periodic background task"""
        while True:
            with contextlib.suppress(Exception):
                # sleep for specified time interval, default: 60s
                time.sleep(int(kwargs.get("interval", 60)))

                if kwargs.get("entity", None) is None:
                    continue

                # send event garbage-collection
                kwargs["entity"].create_default_items()

    def create_default_items(self):
        """create default session contexts if non-existing"""

        if not pathlib.Path(self.config_file).exists():
            return

        config = None
        with pathlib.Path(self.config_file).open(encoding="ascii") as file:
            config = yaml.load(file, Loader=yaml.SafeLoader)

        if config is None:
            return

        shards = {shard.name: shard for shard in self._upsf.list_shards()}

        for entry in config.get("upsf", {}).get("sessionContexts", []):
            for param in ("name",):
                if param not in entry:
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "parameter not found, ignoring entry",
                            "param": param,
                            "entry": entry,
                        }
                    )
                    break
            else:
                # any services defined? create multiple session contexts
                if entry.get("services", []):
                    for item in entry.get("services", []):
                        # make ephemeral copy from original entry
                        _entry = entry.copy()

                        # replace keys in entry with service specific ones
                        _entry["circuitId"] = item.get(
                            "circuitId", entry.get("circuitId", "")
                        )
                        _entry["remoteId"] = item.get(
                            "remoteId", entry.get("remoteId", "")
                        )
                        _entry["sourceMacAddress"] = item.get(
                            "sourceMacAddress", entry.get("sourceMacAddress", "")
                        )
                        _entry["svlan"] = item.get("svlan", entry.get("svlan", "0"))
                        _entry["cvlan"] = item.get("cvlan", entry.get("cvlan", "0"))
                        _entry["shard"] = item.get("shard", entry.get("shard", ""))
                        _entry["requiredServiceGroups"] = item.get(
                            "requiredServiceGroups",
                            entry.get("requiredServiceGroups", []),
                        )
                        _entry["requiredQuality"] = item.get(
                            "requiredQuality", entry.get("requiredQuality", 0)
                        )

                        self.create_session_context(
                            _entry,
                            shards,
                        )

                # no services, single session context
                else:
                    self.create_session_context(entry, shards)

    def stop(self):
        """signals background thread a stop condition"""
        self.log.debug(
            {
                "entity": str(self),
                "event": "thread terminating ...",
            }
        )
        self._stop_thread.set()
        self.join()

    def run(self):
        """runs main loop as background thread"""
        while not self._stop_thread.is_set():
            with contextlib.suppress(Exception):
                try:
                    upsf_subscriber = UPSF(
                        upsf_host=self.upsf_host,
                        upsf_port=self.upsf_port,
                    )

                    for item in upsf_subscriber.read(
                        # subscribe to up, tsf
                        itemtypes=[
                            "shard",
                            "session_context",
                        ],
                        watch=True,
                    ):
                        # with contextlib.suppress(Exception):
                        try:
                            # shards
                            if item.shard.name not in ("",):
                                self.map_all_sessions()

                            # session contexts
                            elif item.session_context.name not in ("",):
                                self.map_session(item.session_context)

                        except UpsfError as error:
                            self.log.error(
                                {
                                    "entity": str(self),
                                    "event": "error occurred",
                                    "error": error,
                                }
                            )

                        if self._stop_thread.is_set():
                            break

                except UpsfError as error:
                    self.log.error(
                        {
                            "entity": str(self),
                            "event": "error occurred",
                            "error": error,
                        }
                    )
                    time.sleep(1)


def parse_arguments(defaults, loglevels):
    """parse command line arguments"""
    parser = argparse.ArgumentParser(sys.argv[0])

    parser.add_argument(
        "--upsf-host",
        help=f'upsf grpc host (default: {defaults["upsf_host"]})',
        dest="upsf_host",
        action="store",
        default=defaults["upsf_host"],
        type=str,
    )

    parser.add_argument(
        "--upsf-port",
        "-p",
        help=f'upsf grpc port (default: {defaults["upsf_port"]})',
        dest="upsf_port",
        action="store",
        default=defaults["upsf_port"],
        type=int,
    )

    parser.add_argument(
        "--config-file",
        "--conf",
        "-c",
        help=f'configuration file (default: {defaults["config_file"]})',
        dest="config_file",
        action="store",
        default=defaults["config_file"],
        type=str,
    )

    parser.add_argument(
        "--default-shard-name",
        help=f'default shard name (default: {defaults["default_shard_name"]})',
        dest="default_shard_name",
        action="store",
        default=defaults["default_shard_name"],
        type=str,
    )

    parser.add_argument(
        "--default-required-quality",
        help=f'default required quality (default: {defaults["default_required_quality"]})',
        dest="default_required_quality",
        action="store",
        default=defaults["default_required_quality"],
        type=int,
    )

    parser.add_argument(
        "--default-required-service-groups",
        "--rsgs",
        help="default required service groups (default: "
        f'{defaults["default_required_service_groups"]})',
        dest="default_required_service_groups",
        action="store",
        default=defaults["default_required_service_groups"],
        type=str,
    )

    parser.add_argument(
        "--registration-interval",
        "-i",
        help=f'registration interval (default: {defaults["registration_interval"]})',
        dest="registration_interval",
        action="store",
        default=defaults["registration_interval"],
        type=int,
    )

    parser.add_argument(
        "--upsf-auto-register",
        "-a",
        help="enable registration of pre-defined shards "
        f'(default: {defaults["upsf_auto_register"]})',
        dest="upsf_auto_register",
        action="store",
        default=defaults["upsf_auto_register"],
        type=str,
    )

    parser.add_argument(
        "--loglevel",
        "-l",
        help=f'set log level (default: {defaults["loglevel"]})',
        dest="loglevel",
        choices=loglevels.keys(),
        action="store",
        default=defaults["loglevel"],
        type=str,
    )

    return parser.parse_args(sys.argv[1:])


def main():
    """main routine"""
    defaults = {
        # upsf host, default: 127.0.0.1
        "upsf_host": os.environ.get("UPSF_HOST", "127.0.0.1"),
        # upsf port, default: 50051
        "upsf_port": os.environ.get("UPSF_PORT", 50051),
        # configuration file, default: /etc/upsf/policy.yaml
        "config_file": os.environ.get("CONFIG_FILE", "/etc/upsf/policy.yaml"),
        # default shard name, default: default-shard
        "default_shard_name": os.environ.get("DEFAULT_SHARD_NAME", "default-shard"),
        # default required quality, default: 1000
        "default_required_quality": os.environ.get("DEFAULT_REQUIRED_QUALITY", 1000),
        # default required service groups, default: basic-internet (comma separated string)
        "default_required_service_groups": os.environ.get(
            "DEFAULT_REQUIRED_SERVICE_GROUPS", "basic-internet"
        ),
        # periodic background thread: time interval
        "registration_interval": os.environ.get("REGISTRATION_INTERVAL", 60),
        # register shards periodically
        "upsf_auto_register": os.environ.get("UPSF_AUTO_REGISTER", "yes"),
        # loglevel, default: 'info'
        "loglevel": os.environ.get("LOGLEVEL", "info"),
    }

    loglevels = {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
        "debug": logging.DEBUG,
    }

    args = parse_arguments(defaults, loglevels)

    # configure logging, here: root logger
    log = logging.getLogger("")

    # add StreamHandler
    hnd = logging.StreamHandler()
    formatter = logging.Formatter(
        f"%(asctime)s: [%(levelname)s] host: {socket.gethostname()}, "
        f"process: {sys.argv[0]}, "
        "module: %(module)s, "
        "func: %(funcName)s, "
        "trace: %(exc_text)s, "
        "message: %(message)s"
    )
    hnd.setFormatter(formatter)
    hnd.setLevel(loglevels[args.loglevel])
    log.addHandler(hnd)

    # set log level of root logger
    log.setLevel(loglevels[args.loglevel])

    # keyword arguments
    kwargs = vars(args)

    # log to debug channel
    log.debug(kwargs)

    shardmgr = SessionContextManager(**kwargs)
    shardmgr.start()
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
