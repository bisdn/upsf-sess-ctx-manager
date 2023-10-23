# upsf-sess-ctx-manager

This repository contains a Python based application named **session
context manager** for grouping session contexts in so-called
subscriber groups mapped to service gateway user planes within a
BBF WT-474 compliant User Plane Selection Function (UPSF).

Its main purpose is to subscribe and listen on events emitted by the
UPSF via its gRPC streaming interface, and maps unassigned session contexts
to one of the available service gateway user planes based on current load
and capacity.

In addition, session context manager may be used for creating session
context entities within the UPSF. It runs a periodic background
task that reads pre-defined session contexts from an associated
policy file and if missing in the UPSF, creates those instances.

See next table for a list of command line options supported by
session context manager. An associated environment variable exists
for each command line option: the CLI option takes precedence,
though.

| Option                            | Default value        | Environment variable            | Description                                                                                      |
|-----------------------------------|----------------------|---------------------------------|--------------------------------------------------------------------------------------------------|
| --upsf-host                       | 127.0.0.1            | UPSF_HOST                       | UPSF server host to connect to                                                                   |
| --upsf-port                       | 50051                | UPSF_PORT                       | UPSF server port to connect to                                                                   |
| --default-shard-name              | default-shard        | DEFAULT_SHARD_NAME              | Default shard name (not in use)                                                                  |
| --default-required-quality        | 1000                 | DEFAULT_REQUIRED_QUALITY        | Default required quality assigned to pre-defined session contexts                                |
| --default-required-service-groups | basic-internet       | DEFAULT_REQUIRED_SERVICE_GROUPS | Default required service groups assigned to pre-defined session contexts, comma separated string |
| --config-file                     | /etc/upsf/policy.yml | CONFIG_FILE                     | Policy configuration file containing pre-defined session contexts                                |
| --registration-interval           | 60                   | REGISTRATION_INTERVAL           | Run periodic background thread every _registration_interval_ seconds.                            |
| --upsf-auto-register              | yes                  | UPSF_AUTO_REGISTER              | Enable periodic background thread for creating pre-defined shards.                               |
| --log-level                       | info                 | LOG_LEVEL                       | Default loglevel, supported options: info, warning, error, critical, debug                       |

This application makes use of the [upsf-client](https://github.com/bisdn/upsf-client)
library for UPSF related communication.

## Getting started and installation

Installation is based on [Setuptools](https://setuptools.pypa.io/en/latest/setuptools.html).

For safe testing create and enter a virtual environment, build and install the
application, e.g.:

```bash
sh# cd upsf-sess-ctx-manager
sh# python3 -m venv venv
sh# source venv/bin/activate
sh# pip install -r ./requirements.txt
sh# python3 setup.py build && python3 setup.py install

### if you haven't done so yet, build and install the submodule(s) as well
sh# git submodule update --init --recursive
sh# cd upsf-client
sh# pip install -r ./requirements.txt
sh# python3 setup.py build && python3 setup.py install

sh# upsf-sess-ctx-manager -h
```

## Mapping session contexts

This section describes briefly the mapping algorithm in session context
manager. It listens on events emitted by the UPSF for session
contexts (SCTX) and subcriber groups (SHRD).

For any event received for these items the mapping algorithm is
executed. Here its pseudo code:

1. Read all SGUP and SHRD items from UPSF.
1. If no SGUP instances exist, return
1. For every session context:
   - set required quality to default value unless specified
   - set list of required service groups to default list unless specified
1. If desired shard is unset and the set of required service groups is not
   empty:
   - get set of candidate SGUP instances based on required and supported
     service groups
   - get active load for each candidate SGUP based on number of allocated sessions
     and maximum number of supported sessions
   - Select SGUP with least load
1. Get all shards assigned to selected least loaded SGUP instance:
   - If the set of shard candidates is empty, return
   - get active load for each candidate shard based on number of allocated
     sessions and maximum number of supported sessions
   - Select shard with least load and assign session context
1. Continue until list of session contexts has been exhausted

If no shard instance can be found for a particular session context,
sess-ctx-manager ignores the session context without altering its
state in the UPSF database.

## Policy configuration file

A configuration file is used for creating pre-defined entities in
the UPSF. A background task ensures existence of those entities,
but will not alter them unless the entity does not exist in the
UPSF yet. Existing entities with or without changes applied by other
UPSF clients will not be altered by session context manager. For re-creating
the original entity as defined in the policy file, you must remove
the item from the UPSF first and sess-ctx-manager's background task will
recreate it after a short period of time.

See below for an example policy configuration or inspect the examples
in the [tools/](./tools/policy.yml) directory:

```yaml
upsf:
  sessionContexts:
    - name: "minimalistic"
      sourceMacAddress: "ee:ee:ee:ee:ee:ee"
    - name: "default-cp-ctx"
      requiredQuality: 1000
      requiredServiceGroups:
        - "basic-internet"
    - name: "acme"
      requiredQuality: 100
      requiredServiceGroups:
        - "acme"
      customerType: "business"
      circuitId: "port18.msan4.bln03.operator.org"
      services:
        - svlan: 3
          requiredServiceGroups:
            - "dev-mgmt"
        - svlan: 7
          requiredServiceGroups:
            - "auth-generic"
        - svlan: 3001
        - svlan: 3002
        - svlan: 3050
        - sourceMacAddress: "52:54:52:54:52:54"
          shard: "shard-F"
          remoteId: "Fritzbox"
        - svlan: 3999
    - name: "customerA@example.org"
      circuitId: "port16.msan03.bln04.operator.org"
      requiredServiceGroups:
        - "basic-internet"
```
