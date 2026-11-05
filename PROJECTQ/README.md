# PROJECTQ — integration with Hydris (Project Q)

Material for connecting our system (fiber sensor + WAKEUPeSTALTA mats) to **Hydris**, the
integration platform by Project Q (https://www.project-q.ai/).

Folder contents:

| File | What it contains |
|---|---|
| `README.md` | this summary: what Hydris is, how we connect, how our system maps onto it, open points |
| `sources.md` | links to the official documentation, page by page |
| `draft_entities.yaml` | **superseded draft** of how fiber, mats and an alarm would look as Hydris entities |
| `hydrisIntegrator.py` | the program: fiber and mats to Hydris (lighter version of `3main.py`, single file) |
| `configuration.json` | coordinates (still fake), thresholds, timing, ESP and Hydris addresses |

Start it with Hydris already open and `3main.py` closed (they use the same UDP port):

```bash
python PROJECTQ/hydrisIntegrator.py
```

Recordings go to `PROJECTQ/archive/` (about 4 GB per day, excluded from git). Sessions recorded
before 05/11/2026 keep their original Italian file names and field names.

The master firmware speaks Italian (`PARLA`, `ZITTO`, `STATO`, `Nodo N: ...`). Those strings are
its protocol, so they appear in the code and in the `master` lines of the event log.

## What you see in Hydris

| Where | Entity | Appearance |
|---|---|---|
| Assets, map | Fiber and mats at rest | blue circle |
| Map | Mat while pressed | red diamond, back to blue on release |
| Tracks | Fiber alarm | yellow, tag "Unknown" |
| Tracks | Mat alarm | red, tag "Red" |

The Assets list always says "Unclassified": the color only shows on the map. Tested on 05/11/2026.
Alarms expire by themselves about 60 s after the last renewal.

Tested on 04/11/2026 on Hydris v1.0.0: an alarm enters the Tracks list only if it has both `track`
and `symbol`, and Hydris computes no symbol for the `other` taxonomy, so hydrisIntegrator sets it by
hand. This overrides the "no symbol on detections" rule in §3. The sections below are the starting
notes from 03/11/2026.

## If the fiber is too sensitive

The fiber alarm fires when the short-term signal power (STA, 0.5 s) exceeds the long-term one
(LTA, 3 s) by `threshold_on`. A fiber alarm also wakes the mats. Try these in order. The first three
are only a change to `configuration.json`, applied at the next start.

1. **Raise `stalta.threshold_on`** (now 1.5). This is the first thing to try. Keep `threshold_off`
   below it (for example 2.0 / 1.5). The ratio is on power, so 2.0 means about 1.4 times the
   amplitude. Of the 19 fiber alarms recorded on 04-05/11/2026, 16 were below 1.75 and 18 below 2.
   Those tests had people moving nearby, so this says what a threshold would filter, not which
   alarms were false.
2. **Raise `stalta.sta_s`** (for example from 0.5 to 1.0 s). Short knocks and gusts weigh less, while
   sustained activity still triggers. Reaction becomes slightly slower.
3. **Raise `stalta.lta_s`** only with care. A longer reference adapts more slowly to slow changes,
   so it can make wind or rain produce more alarms, not fewer. Change it last.
4. **Fix the installation.** Loose cable, wind or vibration on fences, and nearby machinery or roads
   cannot be solved well by software.
5. **Change the program** (needs development):
   - longer settling after start and reset (now 1 s), because several low alarms came 2-7 s after a start or a reset;
   - a minimum duration, or N triggers within T seconds, before alarming;
   - a band-pass filter before STA/LTA, to ignore bands holding only wind or machinery;
   - a pause after each alarm.

**The cost:** the project accepts more false alarms in order not to miss real events. Slow steps put
little energy into the signal, so a higher threshold can miss them, and the mats are not woken either.

**How to choose:** run a few minutes with nobody moving and count the alarms, then repeat with slow
steps and running. Both tests are needed. The recordings in `archive/` allow trying thresholds again
offline without new field work.

---

## 1. Hydris in two lines

- Open source platform (Apache 2.0, European, "no ITAR") that brings sensors, drones, cameras and
  command systems into **a single shared operational map**.
- Runs on the PC (Windows/Mac/Linux, desktop or Docker), offline too.
- Everything is an **entity** (an ID plus "components": position, device, detection,
  classification...). Integrators **push entities** into the "world model" through the API.
- API: **gRPC** (Protocol Buffers) on `localhost:50051`, with ready libraries for TypeScript,
  **Python**, Go, Rust. Alternatively, write a TypeScript **plugin**, distributable to all Hydris
  customers.

## 2. How we connect: two options

| | A. Python gRPC client (recommended to start) | B. Hydris plugin (TypeScript) |
|---|---|---|
| What it is | a script of ours that pushes entities to Hydris when an event occurs | a module running *inside* Hydris |
| Language | Python, like the rest of our project | TypeScript |
| Effort | low: `pip install grpcio protobuf` + `platform_proto` library from GitHub | medium: SDK, build, publishing as an OCI image |
| Distribution | only where we run | "write once, run on every Hydris", through Project Q's review |

Choice: **A for the prototype** (this is `hydrisIntegrator.py`). Move to B only if Project Q wants
our sensor in their catalog.

## 3. What kind of sensor we are for Hydris

The documentation has a case that is exactly ours: **"Perimeter Sensors"** (Entity Cookbook). A
sensor that knows *that* something is nearby, but neither direction nor distance. It is modelled as a
detection with zero offset and a large uncertainty (covariance) around the sensor. The same pattern
applies to tripwires, door contacts and alarms.

Proposed mapping:

| Our system | Hydris entity | Main components |
|---|---|---|
| Interrogator + fiber | **sensor** (asset) | `geo` (reference point), `device` (state Active/Degraded/Failed), `classification` equipment.sensor, fiber shape as a `shape` entity (line) for coverage |
| Mat 1, 2... | one **sensor** each | mat `geo` (exact point), `device` (state: cut cable = Degraded/Failed) |
| Fiber event (STA/LTA) | **detection** | `detectorEntityId` = fiber, `confidence`, `pose` with large covariance (along the fiber), `lifetime` (fresh/until) |
| Mat N pressed | **detection** | `detectorEntityId` = mat N, small covariance (1-2 m): here we know the position |
| Classifier class (steps, running, vehicle...) | detection `classification.taxonomy` | e.g. `person`, `vehicle.ground`; `pending: true` if confidence is low |
| Event WAV | **artifact** (`audio/wav`) | referenced by `detection.evidence` |
| RMS, STA/LTA ratio, chain status | `metric` on the sensor | live readings, with `alerting` to trigger the warning in the UI |

Rules to follow (from the documentation):
- **Deterministic IDs**, for example `knx.fiber1~det~<timestamp>` or `knx.mat2~det~latest`: repeating
  the push updates the entity instead of creating new ones.
- `lifetime.fresh` + `lifetime.until` on every detection: it expires by itself if not renewed.
- `priority: PriorityImmediate` on detections.
- No symbol on detections: Hydris decides from the `taxonomy` (superseded for us, see above).
- A push replaces **whole components**: always send all the fields of a component.

## 4. Open points

1. **GPS coordinates:** we need the fiber path (lat/lon points) and the position of each mat and of
   the interrogator. `configuration.json` still holds fake ones.
2. **The fiber does not localize:** the interrogator says *that* there is an event, not *where* along
   the fiber. In Hydris the fiber alarm appears as an uncertainty area over the whole fiber; the mats
   give a precise point. This must be explained clearly to Project Q.
3. **Class taxonomy:** our classes (steps, running, group, engine/vehicle, ambient noise...) must be
   translated into the Hydris taxonomy. See the Component Reference and the `.proto` files.
4. **Where Hydris runs:** on the same PC as the interrogator (localhost) or on a customer
   server/network? This changes the address and connection security (Hydris currently reports
   "INSECURE", and the examples use an unencrypted channel).
5. **Classifier:** hydrisIntegrator covers only fiber detection and mat alarms. MLtoDL is not
   included.
6. **Contacts:** for official integration (catalog, plugin review) Project Q indicates "Request an
   integration" on the website and `info@project-q.ai`.
