"""Fresh integration tests for the scenario / room HTTP API.

The previous file used the old dict-shaped scenario schema (devices as a dict
of {name: {groups: ...}}) which ScenarioDefinition.model_validate now rejects,
and hit endpoint shapes that have since shifted. Rewritten against the current
API surface using the typed Pydantic models and the routers' initialize()
hooks. Coverage:

  - GET /scenario/definition/{id} — happy path + 404
  - GET /scenario/definition — list all
  - POST /scenario/switch — happy path + 404
  - POST /scenario/role_action — happy path + no-active-scenario
  - GET /scenario/state — happy path + 404 (no active scenario)
  - GET /room/list, GET /room/{id} — happy path + 404
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from locveil_bridge.app import app as main_app
from locveil_bridge.presentation.api.routers import scenarios, rooms, state
from locveil_bridge.domain.scenarios.models import (
    ScenarioDefinition,
    ScenarioState,
    DeviceState,
    RoomDefinition,
)
from locveil_bridge.domain.scenarios.scenario import Scenario, ScenarioError


pytestmark = pytest.mark.integration


# Scenario JSONs that pass current ScenarioDefinition validation (thin format).
SAMPLE_SCENARIOS = {
    "movie_night": {
        "scenario_id": "movie_night",
        "names": {"ru": "Кино", "en": "Movie Night"},
        "description": "Optimal settings for watching movies",
        "room_id": "living_room",
        "roles": {"screen": "tv", "audio": "soundbar"},
        "devices": ["tv", "soundbar"],
        "source": "tv",
        "display": "tv",
        "audio": "soundbar",
    },
    "reading_mode": {
        "scenario_id": "reading_mode",
        "names": {"ru": "Чтение", "en": "Reading Mode"},
        "description": "Comfortable lighting for reading",
        "room_id": "living_room",
        "roles": {"lighting": "lights"},
        "devices": ["lights"],
        "source": "lights",
    },
}


class _MockScenarioManager:
    """Mock ScenarioManager presenting the attributes the routers expect."""

    def __init__(self):
        self.scenario_definitions = {
            sid: ScenarioDefinition.model_validate(data)
            for sid, data in SAMPLE_SCENARIOS.items()
        }
        self.scenario_map = {
            sid: Scenario(self.scenario_definitions[sid], MagicMock())
            for sid in SAMPLE_SCENARIOS
        }
        self.active: dict[str, object] = {}  # room_id -> Scenario (per-room since SCN-6)
        self._live_state: ScenarioState | None = None
        # SCN-19: the chokepoint answers the job result dict (`job_id` rides the response).
        self.switch_scenario = AsyncMock(return_value={
            "success": True, "powered_off": [], "failures": [], "job_id": "j-test-0001", "no_op": False,
        })
        self.execute_role_action = AsyncMock(return_value={"status": "success"})
        self.start_scenario = AsyncMock()
        self.shutdown = AsyncMock()

    def set_active(self, scenario_id: str):
        if scenario_id not in self.scenario_map:
            return
        room = self.scenario_definitions[scenario_id].room_id
        self.active[room] = self.scenario_map[scenario_id]
        self._live_state = ScenarioState(
            scenario_id=scenario_id,
            devices={
                "tv": DeviceState(power=True, input="hdmi1"),
                "soundbar": DeviceState(power=True),
            },
        )

    def active_in_room(self, room_id):
        return self.active.get(room_id)

    def find_role_owner(self, role):
        matches = [sc for sc in self.active.values() if role in sc.definition.roles]
        return matches[0] if len(matches) == 1 else None

    def get_scenario_state(self, scenario_id: str) -> ScenarioState:
        if not self._live_state or self._live_state.scenario_id != scenario_id:
            return ScenarioState(scenario_id=scenario_id, devices={})
        return self._live_state


class _MockRoomManager:
    """Mock RoomManager returning typed RoomDefinition objects."""

    def __init__(self):
        self._rooms = {
            "living_room": RoomDefinition(
                room_id="living_room",
                names={"en": "Living Room"},
                description="Main living area",
                devices=["tv", "soundbar", "lights"],
                default_scenario="movie_night",
            ),
        }

    def get(self, room_id: str):
        return self._rooms.get(room_id)

    def list(self):
        return list(self._rooms.values())

    def contains_device(self, room_id: str, device_id: str) -> bool:
        room = self._rooms.get(room_id)
        return room is not None and device_id in room.devices


@pytest.fixture
def mock_scenario_manager():
    return _MockScenarioManager()


@pytest.fixture
def mock_room_manager():
    return _MockRoomManager()


@pytest.fixture
def mock_mqtt_client():
    m = MagicMock()
    m.publish = AsyncMock()
    return m


@pytest.fixture
def client(mock_scenario_manager, mock_room_manager, mock_mqtt_client):
    """FastAPI TestClient wired with mocks via each router's initialize() hook."""
    scenarios.initialize(mock_scenario_manager, mock_room_manager, mock_mqtt_client)
    rooms.initialize(mock_room_manager)
    # state.initialize(config_manager, device_manager, state_store, scenario_manager).
    state.initialize(MagicMock(), MagicMock(), MagicMock(), mock_scenario_manager)
    return TestClient(main_app)


# --- /scenario/definition ----------------------------------------------------


def test_get_scenario_definition_success(client):
    response = client.get("/scenario/definition/movie_night")
    assert response.status_code == 200
    data = response.json()
    assert data["scenario_id"] == "movie_night"
    assert data["names"]["en"] == "Movie Night"
    assert data["room_id"] == "living_room"
    assert "roles" in data
    assert "devices" in data
    assert data["source"] == "tv"
    assert data["display"] == "tv"
    assert data["audio"] == "soundbar"


def test_get_scenario_definition_not_found(client):
    response = client.get("/scenario/definition/nonexistent")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_list_scenario_definitions(client):
    response = client.get("/scenario/definition")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    ids = {entry["scenario_id"] for entry in data}
    assert ids == {"movie_night", "reading_mode"}


# --- /scenario/switch --------------------------------------------------------


def test_switch_scenario_success(client, mock_scenario_manager):
    response = client.post("/scenario/switch", json={"id": "reading_mode", "graceful": True})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert "reading_mode" in body["message"]
    assert body["job_id"] == "j-test-0001"
    mock_scenario_manager.switch_scenario.assert_awaited_once_with("reading_mode", graceful=True)


def test_switch_scenario_not_found(client, mock_scenario_manager):
    """If the manager raises ValueError (e.g., unknown scenario id), the API returns 404."""
    mock_scenario_manager.switch_scenario.side_effect = ValueError("Scenario 'nope' not found")
    response = client.post("/scenario/switch", json={"id": "nope", "graceful": True})
    assert response.status_code == 404


# --- /scenario/role_action --------------------------------------------------


def test_role_action_success(client, mock_scenario_manager):
    """role_action delegates to scenario_manager.execute_role_action and wraps the result.

    The API envelope is `{"status": "success", "result": <manager-return>}`; the
    inner manager-return is whatever execute_role_action returned (our mock yields
    `{"status": "success"}` which lands under the 'result' key).
    """
    mock_scenario_manager.set_active("movie_night")
    response = client.post(
        "/scenario/role_action",
        json={"role": "screen", "command": "power_on", "params": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["result"] == {"status": "success"}
    mock_scenario_manager.execute_role_action.assert_awaited_once_with(
        "screen", "power_on", {}
    )


def test_role_action_no_active_scenario(client, mock_scenario_manager):
    """execute_role_action raises ScenarioError('No scenario is currently active'); API surfaces 4xx."""
    mock_scenario_manager.execute_role_action.side_effect = ScenarioError(
        "No scenario is currently active", "no_active_scenario", True
    )
    response = client.post(
        "/scenario/role_action",
        json={"role": "screen", "command": "power_on", "params": {}},
    )
    assert response.status_code in (400, 404, 409, 500)


# --- /scenario/state ---------------------------------------------------------


def test_get_scenario_state_success(client, mock_scenario_manager):
    mock_scenario_manager.set_active("movie_night")
    response = client.get("/scenario/state")
    assert response.status_code == 200
    data = response.json()
    assert data["scenario_id"] == "movie_night"
    assert "devices" in data
    assert data["devices"]["tv"]["power"] is True


def test_get_scenario_state_no_active(client, mock_scenario_manager):
    """No active scenario -> 404 (or another 4xx)."""
    mock_scenario_manager.active = {}
    mock_scenario_manager._live_state = None
    response = client.get("/scenario/state")
    assert response.status_code in (404, 400)


# --- /room -------------------------------------------------------------------


def test_list_rooms(client):
    response = client.get("/room/list")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    ids = {entry["room_id"] for entry in data}
    assert "living_room" in ids


def test_get_room_success(client):
    response = client.get("/room/living_room")
    assert response.status_code == 200
    data = response.json()
    assert data["room_id"] == "living_room"
    assert data["devices"] == ["tv", "soundbar", "lights"]


def test_get_room_not_found(client):
    response = client.get("/room/no_such_room")
    assert response.status_code == 404
