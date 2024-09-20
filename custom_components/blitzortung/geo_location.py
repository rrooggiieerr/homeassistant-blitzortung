"""Support for Blitzortung geo location events."""
import bisect
import logging
import time
import uuid

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    UnitOfLength
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.util.dt import utc_from_timestamp
from homeassistant.util.unit_system import IMPERIAL_SYSTEM

from . import BlitzortungConfigEntry
from .const import ATTR_EXTERNAL_ID, ATTR_PUBLICATION_DATE, ATTRIBUTION, DOMAIN

_LOGGER = logging.getLogger(__name__)


DEFAULT_EVENT_NAME_TEMPLATE = "Lightning Strike"

SIGNAL_DELETE_ENTITY = "blitzortung_delete_entity_{0}"


async def async_setup_entry(hass, config_entry: BlitzortungConfigEntry, async_add_entities):
    coordinator = config_entry.runtime_data
    if not coordinator.max_tracked_lightnings:
        return

    manager = BlitzortungEventManager(
        hass,
        async_add_entities,
        coordinator.max_tracked_lightnings,
        coordinator.time_window_seconds,
    )

    coordinator.register_lightning_receiver(manager.lightning_cb)
    coordinator.register_on_tick(manager.tick)


class Strikes(list):
    def __init__(self, capacity):
        self._keys = []
        self._key_fn = lambda strike: strike._publication_date
        self._max_key = 0
        self._capacity = capacity
        super().__init__()

    def insort(self, item):
        k = self._key_fn(item)
        if k > self._max_key:
            self._max_key = k
            self._keys.append(k)
            self.append(item)
        else:
            i = bisect.bisect_right(self._keys, k)
            self._keys.insert(i, k)
            self.insert(i, item)
        n = len(self) - self._capacity
        if n > 0:
            del self._keys[0:n]
            to_delete = self[0:n]
            self[0:n] = []
            return to_delete
        return ()

    def cleanup(self, k):
        if not self._keys or self._keys[0] > k:
            return ()

        i = bisect.bisect_right(self._keys, k)
        if not i:
            return ()

        del self._keys[0:i]
        to_delete = self[0:i]
        self[0:i] = []
        return to_delete


class BlitzortungEventManager:
    """Define a class to handle Blitzortung events."""

    def __init__(
        self, hass, async_add_entities, max_tracked_lightnings, window_seconds,
    ):
        """Initialize."""
        self._async_add_entities = async_add_entities
        self._hass = hass
        self._strikes = Strikes(max_tracked_lightnings)
        self._window_seconds = window_seconds

        if hass.config.units == IMPERIAL_SYSTEM:
            self._unit = UnitOfLength.MILES
        else:
            self._unit = UnitOfLength.KILOMETERS

    async def lightning_cb(self, lightning):
        _LOGGER.debug("geo_location lightning: %s", lightning)
        event = BlitzortungEvent(
            lightning["distance"],
            lightning["lat"],
            lightning["lon"],
            self._unit,
            lightning["time"],
            lightning["status"],
            lightning["region"],
        )
        to_delete = self._strikes.insort(event)
        self._async_add_entities([event])
        if to_delete:
            self._remove_events(to_delete)
        _LOGGER.debug("tracked lightnings: %s", len(self._strikes))

    @callback
    def _remove_events(self, events):
        """Remove old geo location events."""
        _LOGGER.debug("Going to remove %s", events)
        for event in events:
            async_dispatcher_send(
                self._hass, SIGNAL_DELETE_ENTITY.format(event._strike_id)
            )

    def tick(self):
        to_delete = self._strikes.cleanup(time.time() - self._window_seconds)
        if to_delete:
            self._remove_events(to_delete)


class BlitzortungEvent(GeolocationEvent):
    """Define a lightning strike event."""

    def __init__(self, distance, latitude, longitude, unit, time, status, region):
        """Initialize entity with data provided."""
        self._attr_distance = distance
        self._attr_latitude = latitude
        self._attr_longitude = longitude
        self._attr_attribution = ATTRIBUTION
        self._attr_extra_state_attributes = {
            ATTR_EXTERNAL_ID: self._strike_id,
            ATTR_PUBLICATION_DATE: utc_from_timestamp(self._publication_date),
        }
        self._time = time
        self._status = status
        self._region = region
        self._publication_date = time / 1e9
        self._remove_signal_delete = None
        self._strike_id = str(uuid.uuid4()).replace("-", "")
        self._attr_unit_of_measurement = unit
        self._attr_icon = "mdi:flash"
        self._attr_source = DOMAIN
        self._attr_should_poll = False
        self.entity_id = f"geo_location.lightning_strike_{self._strike_id}"

    @property
    def name(self):
        """Return the name of the event."""
        return DEFAULT_EVENT_NAME_TEMPLATE.format(self._publication_date)

    @callback
    def _delete_callback(self):
        """Remove this entity."""
        self._remove_signal_delete()
        self.hass.async_create_task(self.async_remove())

    async def async_added_to_hass(self):
        """Call when entity is added to hass."""
        self._remove_signal_delete = async_dispatcher_connect(
            self.hass,
            SIGNAL_DELETE_ENTITY.format(self._strike_id),
            self._delete_callback,
        )
