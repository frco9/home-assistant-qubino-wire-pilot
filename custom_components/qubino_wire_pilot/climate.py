"""Adds support for TPI thermostat units."""
import asyncio
import logging
import math

import voluptuous as vol
from datetime import time, timedelta

from homeassistant.components import light
from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    CURRENT_HVAC_COOL,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    PRESET_AWAY,
    PRESET_NONE,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, CoreState, callback
from homeassistant.exceptions import ConditionError
from homeassistant.helpers import condition
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 0.3
DEFAULT_NAME = "Generic Thermostat"
DEFAULT_T_COEFF = 0.01
DEFAULT_C_COEFF = 0.6
DEFAULT_TARGET_TEMPERATURE = 20

VALUE_OFF = 10
VALUE_FROST = 20
VALUE_ECO = 30
VALUE_COMFORT_2 = 40
VALUE_COMFORT_1 = 50
VALUE_COMFORT = 99

CONF_HEATER = "heater"
CONF_IN_TEMP_SENSOR = 'in_temperature_sensor'
CONF_OUT_TEMP_SENSOR = 'out_temperature_sensor'
CONF_WINDOWS_SENSOR = 'window_sensor'
CONF_WINDOWS_DELAY = 'window_delay'
CONF_C_COEFF = 'c_coefficient'
CONF_T_COEFF = 't_coefficient'
CONF_EVAL_TIME = 'eval_time_s'
CONF_TARGET_TEMP = "target_temp"
CONF_AC_MODE = "ac_mode"
CONF_INITIAL_HVAC_MODE = "initial_hvac_mode"
CONF_AWAY_TEMP = "away_temp"
CONF_PRECISION = "precision"
SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HEATER): cv.entity_id,
        vol.Required(CONF_IN_TEMP_SENSOR): cv.entity_id,
        vol.Required(CONF_OUT_TEMP_SENSOR): cv.entity_id,
        vol.Required(CONF_WINDOWS_SENSOR): cv.entity_id,
        vol.Optional(CONF_WINDOWS_DELAY): vol.Coerce(float),
        vol.Optional(CONF_T_COEFF, default=DEFAULT_T_COEFF): vol.Coerce(float),
        vol.Optional(CONF_C_COEFF, default=DEFAULT_C_COEFF): vol.Coerce(float),
        vol.Optional(CONF_EVAL_TIME): vol.Coerce(int),
        vol.Optional(CONF_AC_MODE): cv.boolean,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [HVAC_MODE_COOL, HVAC_MODE_HEAT, HVAC_MODE_OFF]
        ),
        vol.Optional(CONF_AWAY_TEMP): vol.Coerce(float),
        vol.Optional(CONF_PRECISION): vol.In(
            [PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE]
        ),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the generic thermostat platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config.get(CONF_NAME)
    heater_entity_id = config.get(CONF_HEATER)
    in_temp_sensor_entity_id = config.get(CONF_IN_TEMP_SENSOR)
    out_temp_sensor_entity_id = config.get(CONF_OUT_TEMP_SENSOR)
    window_sensor_entity_id = config.get(CONF_WINDOWS_SENSOR)
    window_delay = config.get(CONF_WINDOWS_DELAY)
    t_coeff = config.get(CONF_T_COEFF)
    c_coeff = config.get(CONF_C_COEFF)
    eval_time_s = config.get(CONF_EVAL_TIME)
    target_temp = config.get(CONF_TARGET_TEMP)
    ac_mode = config.get(CONF_AC_MODE)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    away_temp = config.get(CONF_AWAY_TEMP)
    precision = config.get(CONF_PRECISION)
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)

    async_add_entities(
        [
            TPIThermostat(
                name,
                heater_entity_id,
                in_temp_sensor_entity_id,
                out_temp_sensor_entity_id,
                window_sensor_entity_id,
                window_delay,
                t_coeff,
                c_coeff,
                eval_time_s,
                target_temp,
                ac_mode,
                initial_hvac_mode,
                away_temp,
                precision,
                unit,
                unique_id,
            )
        ]
    )


class TPIThermostat(ClimateEntity, RestoreEntity):
    """Representation of a TPI Thermostat device."""

    def __init__(
        self,
        name,
        heater_entity_id,
        in_temp_sensor_entity_id,
        out_temp_sensor_entity_id,
        window_sensor_entity_id,
        window_delay,
        t_coeff,
        c_coeff,
        eval_time_s,
        target_temp,
        ac_mode,
        initial_hvac_mode,
        away_temp,
        precision,
        unit,
        unique_id,
    ):
        """Initialize the thermostat."""
        self._name = name
        self.heater_entity_id = heater_entity_id
        self.in_temp_sensor_entity_id = in_temp_sensor_entity_id
        self.out_temp_sensor_entity_id = out_temp_sensor_entity_id
        self.window_sensor_entity_id = window_sensor_entity_id
        self.window_delay = window_delay
        self.t_coeff = t_coeff
        self.c_coeff = c_coeff
        self.eval_time_s = eval_time_s
        self.ac_mode = ac_mode
        self._hvac_mode = initial_hvac_mode
        self._saved_target_temp = target_temp or away_temp
        self._temp_precision = precision
        if self.ac_mode:
            self._hvac_list = [HVAC_MODE_COOL, HVAC_MODE_OFF]
        else:
            self._hvac_list = [HVAC_MODE_HEAT, HVAC_MODE_OFF]
        self._active = False
        self._cur_in_temp = None
        self._cur_out_temp = None
        self._cur_power = 0
        self._temp_lock = asyncio.Lock()
        self._target_temp = target_temp
        self._unit = unit
        self._unique_id = unique_id
        self._support_flags = SUPPORT_FLAGS
        if away_temp:
            self._support_flags = SUPPORT_FLAGS | SUPPORT_PRESET_MODE
        self._away_temp = away_temp
        self._is_away = False

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.in_temp_sensor_entity_id], self._async_temp_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.out_temp_sensor_entity_id], self._async_temp_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._async_light_changed
            )
        )

        if self.eval_time_s:
            self._stop_control_loop = async_track_time_interval(
                self.hass, self._async_control_heating, timedelta(seconds=self.eval_time_s)
            )
            self.async_on_remove(self._stop_control_loop)

        @callback
        def _async_startup(*_):
            """Init on startup."""
            in_temp_sensor_state = self.hass.states.get(self.in_temp_sensor_entity_id)
            if in_temp_sensor_state and in_temp_sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self._async_update_temp(self.in_temp_sensor_entity_id, in_temp_sensor_state)
                self.async_write_ha_state()
            
            out_temp_sensor_state = self.hass.states.get(self.out_temp_sensor_entity_id)
            if out_temp_sensor_state and out_temp_sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self._async_update_temp(self.out_temp_sensor_entity_id, out_temp_sensor_state)
                self.async_write_ha_state()
            
            
            light_state = self.hass.states.get(self.heater_entity_id)
            if light_state and light_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self.hass.create_task(self._check_switch_initial_state())

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            # If we have no initial temperature, restore
            if self._target_temp is None:
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self._target_temp = DEFAULT_TARGET_TEMPERATURE
                    _LOGGER.warning(
                        "Undefined target temperature, falling back to %s",
                        self._target_temp,
                    )
                else:
                    self._target_temp = float(old_state.attributes[ATTR_TEMPERATURE])
            if old_state.attributes.get(ATTR_PRESET_MODE) == PRESET_AWAY:
                self._is_away = True
            if not self._hvac_mode and old_state.state:
                self._hvac_mode = old_state.state

        else:
            # No previous state, try and restore defaults
            if self._target_temp is None:
                self._target_temp = DEFAULT_TARGET_TEMPERATURE
            _LOGGER.warning(
                "No previously saved temperature, setting to %s", self._target_temp
            )

        # Set default state to off
        if not self._hvac_mode:
            self._hvac_mode = HVAC_MODE_OFF

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of this thermostat."""
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        # Since this integration does not yet have a step size parameter
        # we have to re-use the precision as the step size for now.
        return self.precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_in_temperature(self):
        """Return the sensor temperature."""
        return self._cur_in_temp

    @property
    def current_out_temperature(self):
        """Return the sensor temperature."""
        return self._cur_out_temp

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        if self._hvac_mode == HVAC_MODE_OFF:
            return CURRENT_HVAC_OFF
        if not self._is_device_active:
            return CURRENT_HVAC_IDLE
        if self.ac_mode:
            return CURRENT_HVAC_COOL
        return CURRENT_HVAC_HEAT

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temp

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._hvac_list

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp."""
        return PRESET_AWAY if self._is_away else PRESET_NONE

    @property
    def preset_modes(self):
        """Return a list of available preset modes or PRESET_NONE if _away_temp is undefined."""
        return [PRESET_NONE, PRESET_AWAY] if self._away_temp else PRESET_NONE

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        if hvac_mode == HVAC_MODE_HEAT:
            self._hvac_mode = HVAC_MODE_HEAT
            await self._async_control_heating()
        elif hvac_mode == HVAC_MODE_COOL:
            self._hvac_mode = HVAC_MODE_COOL
            await self._async_control_heating()
        elif hvac_mode == HVAC_MODE_OFF:
            self._hvac_mode = HVAC_MODE_OFF
            if self._is_device_active:
                await self._async_heater_turn_off()
        else:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return
        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._target_temp = temperature
        await self._update_control_loop()
        self.async_write_ha_state()

    async def _update_control_loop(self):
        """Reset control loop to give its latest state."""
        if self._stop_control_loop:
            self._stop_control_loop() # Call remove callback on the loop 

        # Start a new control loop with updated values
        self._stop_control_loop = async_track_time_interval(
            self.hass, self._async_control_heating, timedelta(seconds=self.eval_time_s)
        )
        self.async_on_remove(self._stop_control_loop)

    async def _async_temp_sensor_changed(self, event):
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        entity_id = event.data.get("entity_id")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self._async_update_temp(entity_id, new_state)
        self.async_write_ha_state()

    async def _check_light_initial_state(self):
        """Prevent the device from keep running if HVAC_MODE_OFF."""
        if self._hvac_mode == HVAC_MODE_OFF and self._is_device_active:
            _LOGGER.warning(
                "The climate mode is OFF, but the switch device is ON. Turning off device %s",
                self.heater_entity_id,
            )
            await self._async_heater_turn_off()

    @callback
    def _async_light_changed(self, event):
        """Handle heater light state changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        if old_state is None:
            self.hass.create_task(self._check_light_initial_state())
        self.async_write_ha_state()

    @callback
    def _async_update_temp(self, entity_id, state):
        """Update thermostat with latest state from sensor."""
        try:
            cur_temp = float(state.state)
            if math.isnan(cur_temp) or math.isinf(cur_temp):
                raise ValueError(f"Sensor has illegal state {state.state}")
            if entity_id == self.in_temp_sensor_entity_id:
                self._cur_in_temp
            elif entity_id == self.out_temp_sensor_entity_id:
                self._cur_out_temp
            else:
                _LOGGER.error("Unable to update from sensor: no matching entity id")
        except ValueError as ex:
            _LOGGER.error("Unable to update from sensor: %s", ex)

    @callback
    def _async_update_power(self):
        """Update power with latest state from sensors."""
        try:
            c = self.c_coeff
            t = self.t_coeff
            target = self.target_temperature
            inside = self._cur_in_temp
            outside = self._cur_out_temp
            power_formula = c * (target - inside) + t * (target - outside)
            cur_power = min(max(power_formula, 0), 1) * 100
            
            self._cur_power = cur_power
        except ValueError as ex:
            _LOGGER.error("Unable to compute power from sensor: %s", ex)

    async def _async_control_heating(self, time=None):
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            if not self._active and None not in (
                self._cur_temp,
                self._target_temp,
            ):
                self._active = True
                _LOGGER.info(
                    "Obtained current and target temperature. "
                    "Generic thermostat active. %s, %s",
                    self._cur_temp,
                    self._target_temp,
                )

            if not self._active or self._hvac_mode == HVAC_MODE_OFF:
                return

            
            _LOGGER.info("Current power %s", self._cur_power)
            heating_delay = self._cur_power * 6

            _LOGGER.info("Turning on heater %s", self.heater_entity_id)
            await self._async_heater_turn_on()

            _LOGGER.info("Waiting for %s", heating_delay)
            await asyncio.sleep(heating_delay)

            _LOGGER.info("Turning off heater %s", self.heater_entity_id)
            await self._async_heater_turn_off()

    @property
    def _is_device_active(self):
        """If the toggleable device is currently active."""
        if not self.hass.states.get(self.heater_entity_id):
            return None

        return self.hass.states.is_state(self.heater_entity_id,
            STATE_ON) and self.hass.states.state_attr(self.heater_entity_id, 'brightness') >= VALUE_COMFORT_2

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    async def _async_heater_turn_on(self):
        """Turn heater device on."""
        self._async_set_heater_value(VALUE_COMFORT)

    async def _async_heater_turn_off(self):
        """Turn heater device off."""
        self._async_set_heater_value(VALUE_OFF)

    async def _async_set_heater_value(self, value):
        """Turn heater toggleable device on."""
        data = {
            ATTR_ENTITY_ID: self.heater_entity_id,
            light.ATTR_BRIGHTNESS: value * 255 / 99,
        }

        await self.hass.services.async_call(
            light.DOMAIN, light.SERVICE_TURN_ON, data, context=self._context
        )

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        if preset_mode == PRESET_AWAY and not self._is_away:
            self._is_away = True
            self._saved_target_temp = self._target_temp
            self._target_temp = self._away_temp
            await self._update_control_loop()
        elif preset_mode == PRESET_NONE and self._is_away:
            self._is_away = False
            self._target_temp = self._saved_target_temp
            await self._update_control_loop()

        self.async_write_ha_state()