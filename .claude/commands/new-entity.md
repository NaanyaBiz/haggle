# /new-entity

Scaffold a new sensor entity with tests.

## Usage

```
/new-entity <key> <translation_key> <device_class> <state_class> <unit>
```

Example:
```
/new-entity consumption_cost consumption_cost MONETARY TOTAL_INCREASING AUD
```

## Steps

1. Add a new `SensorEntityDescription` entry to `SENSOR_DESCRIPTIONS` in `custom_components/haggle/sensor.py` with the provided params.
2. Add the key constant to `custom_components/haggle/const.py` (follow the `DATA_*` naming pattern).
3. Add the translation key to `custom_components/haggle/strings.json` and `custom_components/haggle/translations/en.json`.
4. Add the key to the coordinator's mock return value in `tests/test_init.py` (`_COORDINATOR_DATA`).
5. Invoke the `ha-test-writer` subagent to write a dedicated test for the new sensor.
6. Invoke the `energy-domain-expert` subagent if the entity touches the Energy dashboard (device_class=ENERGY or MONETARY).
7. Run `uv run ruff check --fix custom_components/ tests/ && uv run mypy custom_components/haggle && uv run pytest` and fix any issues before reporting done.
