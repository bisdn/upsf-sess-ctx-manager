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

<table>
  <tr>
    <th>Option</th>
    <th>Default value</th>
    <th>Environment variable</th>
    <th>Description</th>
  </tr>
  <tr>
    <td>--upsf-host</td>
    <td>127.0.0.1</td>
    <td>UPSF_HOST</td>
    <td>UPSF server host to connect to</td>
  </tr>
  <tr>
    <td>--upsf-port</td>
    <td>50051</td>
    <td>UPSF_PORT</td>
    <td>UPSF server port to connect to</td>
  </tr>
  <tr>
    <td>--default-shard-name</td>
    <td>default-shard</td>
    <td>DEFAULT_SHARD_NAME</td>
    <td>Default shard name (not in use)</td>
  </tr>
  <tr>
    <td>--default-required-quality</td>
    <td>1000</td>
    <td>DEFAULT_REQUIRED_QUALITY</td>
    <td>Default required quality assigned to pre-defined session contexts</td>
  </tr>
  <tr>
    <td>--default-required-service-groups</td>
    <td>basic-internet</td>
    <td>DEFAULT_REQUIRED_SERVICE_GROUPS</td>
    <td>Default required service groups assigned to pre-defined session contexts, comma separated string</td>
  </tr>
  <tr>
    <td>--config-file</td>
    <td>/etc/upsf/policy.yml</td>
    <td>CONFIG_FILE</td>
    <td>Policy configuration file containing pre-defined subscriber groups (shards)</td>
  </tr>
  <tr>
    <td>--registration-interval</td>
    <td>60</td>
    <td>REGISTRATION_INTERVAL</td>
    <td>Run periodic background thread every _registration_interval_ seconds.</td>
  </tr>
  <tr>
    <td>--upsf-auto-register</td>
    <td>yes</td>
    <td>UPSF_AUTO_REGISTER</td>
    <td>Enable periodic background thread for creating pre-defined shards.</td>
  </tr>
  <tr>
    <td>--log-level</td>
    <td>info</td>
    <td>LOG_LEVEL</td>
    <td>Default loglevel, supported options: info, warning, error, critical, debug</td>
  </tr>
</table>

This application makes use of the <a
href="https://github.com/bisdn/upsf-client">upsf-client</a> library for
UPSF related communication.

# Getting started and installation

Installation is based on <a
href="https://setuptools.pypa.io/en/latest/setuptools.html">Setuptools</a>.

For safe testing create and enter a virtual environment, build and install the
application, e.g.:

```
sh# cd upsf-sess-ctx-manager
sh# python3 -m venv venv
sh# source venv/bin/activate
sh# python3 setup.py build && python3 setup.py install

### if you haven't done before, build and install the submodules as well
sh# git submodule update --init --recursive
sh# cd upsf-grpc-client
sh# python3 setup.py build && python3 setup.py install

sh# sess-ctx-manager -h
```

# Mapping session contexts

This section describes briefly the mapping algorithm in session context
manager. It listens on events emitted by the UPSF for session
contexts (SCTX) and subcriber groups (SHRD). 

For any event received for these items the mapping algorithm is
executed. Here its pseudo code:

1. Read all SGUP and SHRD items from UPSF.

2. If no SGUP instances exist, return

3. For every session context:
   * set required quality to default value unless specified
   * set list of required service groups to default list unless specified

4. If desired shard is unset and the set of required service groups is not
   empty:
   * get set of candidate SGUP instances based on required and supported 
     service groups
   * get active load for each candidate SGUP based on number of allocated sessions
     and maximum number of supported sessions
   * Select SGUP with least load

5. Get all shards assigned to selected least loaded SGUP instance:
   * If the set of shard candidates is empty, return
   * get active load for each candidate shard based on number of allocated
     sessions and maximum number of supported sessions
   * Select shard with least load and assign session context

6. Continue until list of session contexts has been exhausted

If no shard instance can be found for a particular session context,
sess-ctx-manager ignores the session context without altering its
state in the UPSF database.

# Policy configuration file

A configuration file is used for creating pre-defined entities in
the UPSF. A background task ensures existence of those entities,
but will not alter them unless the entity does not exist in the
UPSF yet. Existing entities with or without changes applied by other
UPSF clients will not be altered by session context manager. For re-creating
the original entity as defined in the policy file, you must remove
the item from the UPSF first and sess-ctx-manager's background task will
recreate it after a short period of time.

See below for an example policy configuration or inspect the examples
in the <a href="./tools/policy.yml">tools/</a> directory:

```
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

