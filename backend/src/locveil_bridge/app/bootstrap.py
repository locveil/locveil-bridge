import os
import re
import time
import logging
import asyncio
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from locveil_bridge.domain.devices.models import (
    BaseDeviceState,
    KitchenHoodState,
    LgTvState,
    WirenboardIRState,
    RevoxA77ReelToReelState,
    AppleTVState,
    AuralicDeviceState,
    EmotivaXMC2State,
    MitsubishiHvacState,
)
from locveil_bridge.app.reload_service import ReloadService
from locveil_bridge.domain.ports import EventPublisherPort
from locveil_bridge.infrastructure.config.manager import ConfigManager
from locveil_bridge.domain.devices.service import DeviceManager
from locveil_bridge.infrastructure.mqtt.client import MQTTClient
from locveil_bridge.infrastructure.persistence.sqlite import SQLiteStateStore
from locveil_bridge.infrastructure.wb_device.service import WBVirtualDeviceService
from locveil_bridge.domain.rooms.service import RoomManager
from locveil_bridge.domain.scenarios.service import ScenarioManager
from locveil_bridge.domain.scenarios.proxy import ScenarioProxy
from locveil_bridge.infrastructure.scenarios.wb_adapter import ScenarioWBAdapter
from locveil_bridge.infrastructure.capabilities.loader import attach_capability_maps, validate_command_exposure
from locveil_bridge.infrastructure.maintenance.wirenboard_guard import WirenboardMaintenanceGuard
from locveil_bridge.infrastructure.reports.github_sink import GitHubReportSink
from locveil_bridge.domain.reports.models import ReportsSettings
from locveil_bridge.domain.reports.rings import DispatchRing, MqttWindow
from locveil_bridge.domain.reports.service import ReportService

# Import routers
from locveil_bridge.presentation.api.routers import (
    system, devices, mqtt, scenarios, rooms, state, events, reports
)
from locveil_bridge.presentation.api.catalog import build_catalog
from locveil_bridge.presentation.api.sse_manager import sse_manager, SSEChannel

from locveil_bridge.__version__ import __version__


# Setup logging. Retention = today + yesterday only (OPS-25, owner decision):
# yesterday's file keeps cross-midnight analysis possible; anything older is
# gone — the WB7 sdcard pays for every retained megabyte. The container's
# stdout copy is separately capped by docker json-file (10m x 3).
LOG_RETENTION_DAYS = 1


def _startup_rollover(log_path: Path) -> None:
    """Rename the previous run's live log aside so each startup begins a fresh file.

    The rotated name stays in the same `<name>.<stamp>.log` family the daily
    rotation uses, so the report-evidence collector's `service.log.*` glob
    (domain/reports/service.py::_collect_logs) sees both kinds of siblings.
    """
    if not log_path.exists():
        return
    try:
        if log_path.stat().st_size == 0:
            return  # nothing worth keeping; reuse the empty file
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rotated = log_path.with_name(f"{log_path.name}.{stamp}.log")
        log_path.rename(rotated)
        print(f"Previous log rotated to: {rotated}")
    except OSError as e:
        # File logging isn't up yet; never block startup on a rename failure.
        print(f"Warning: could not rotate previous log {log_path}: {e}")


def _prune_old_logs(log_path: Path, keep_days: int = LOG_RETENTION_DAYS) -> int:
    """Delete rotated siblings (`<name>.*`) older than the retention window.

    Covers the startup-renamed files, which TimedRotatingFileHandler's own
    backupCount cleanup never matches (its extMatch only knows the daily suffix).
    """
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for sibling in log_path.parent.glob(log_path.name + ".*"):
        try:
            if sibling.stat().st_mtime < cutoff:
                sibling.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def setup_logging(log_file: str, log_level: str):
    """Configure the logging system: fresh file per startup + daily rotation."""
    try:
        # Create logs directory if it doesn't exist
        log_dir = os.path.dirname(log_file)
        Path(log_dir).mkdir(parents=True, exist_ok=True)

        # Each startup gets a fresh file (voice-repo behavior); long-running
        # operation still rotates daily below. Prune anything past retention.
        log_path = Path(log_file)
        _startup_rollover(log_path)
        pruned = _prune_old_logs(log_path)

        # Set up logging format
        log_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Create timed rotating file handler
        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when='midnight',  # Rotate at midnight
            interval=1,       # One day interval
            backupCount=LOG_RETENTION_DAYS,
            encoding='utf-8'
        )

        # Set custom suffix for rotated files — and teach the handler's cleanup
        # to recognize it: getFilesToDelete() filters via extMatch, whose default
        # pattern never matches this suffix, so backupCount deleted nothing.
        file_handler.suffix = "%Y%m%d.log"
        file_handler.extMatch = re.compile(r"^\d{8}\.log$")
        file_handler.setFormatter(log_formatter)
        
        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_formatter)
        
        # Get root logger
        root_logger = logging.getLogger()
        
        # Remove any existing handlers
        root_logger.handlers = []
        
        # Add our handlers
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
        # Set log level
        numeric_level = getattr(logging, log_level.upper(), logging.INFO)
        root_logger.setLevel(numeric_level)
        
        logger = logging.getLogger(__name__)
        logger.info(
            "Logging system initialized (fresh file per startup, daily rotation, "
            "%d-day retention) at level %s", LOG_RETENTION_DAYS, log_level
        )
        if pruned:
            logger.info("Pruned %d rotated log file(s) past retention", pruned)
        
    except Exception as e:
        print(f"Error setting up logging: {str(e)}")
        raise


async def _release_partial_startup(
    report_retry_task: "asyncio.Task | None",
    device_manager: "DeviceManager | None",
    mqtt_client: "MQTTClient | None",
    state_store: "SQLiteStateStore | None",
) -> None:
    """Best-effort release of partially initialized startup resources (OPS-8).

    A rare, unexpected error mid-startup (outside the handled device/scenario
    load paths) used to leak whatever was already up — an open SQLite handle, a
    connected MQTT client, device sockets — leaving a hung process holding its
    ports. Called from the lifespan's startup except-block before re-raising.
    Every step is independently guarded: a failing release must not mask the
    original startup error or stop the remaining releases.
    """
    log = logging.getLogger(__name__)
    if report_retry_task is not None:
        report_retry_task.cancel()
    if device_manager is not None and mqtt_client is not None:
        # Mirror the normal shutdown's WB-card offline pass (meta/error=offline +
        # meta/available=0) while MQTT is still connected — without it, a failure
        # after device setup leaves retained available=1 cards looking live in the
        # WB UI with the bridge down (OPS-18, completing the OPS-8 symmetry).
        from locveil_bridge.infrastructure.devices.base import BaseDevice as _BaseDevice  # local: keep domain/ import-pure
        for port_device in device_manager.devices.values():
            wb_dev = cast(_BaseDevice, port_device)
            try:
                await wb_dev.cleanup_wb_device_state()
            except Exception as e:
                log.warning(
                    f"Startup-failure cleanup: WB offline cleanup failed for "
                    f"{wb_dev.device_id}: {e}"
                )
    if device_manager is not None:
        try:
            await device_manager.shutdown_devices()
        except Exception as e:
            log.warning(f"Startup-failure cleanup: device shutdown failed: {e}")
    if mqtt_client is not None:
        try:
            await mqtt_client.disconnect()
        except Exception as e:
            log.warning(f"Startup-failure cleanup: MQTT disconnect failed: {e}")
    if state_store is not None:
        try:
            await state_store.close()
        except Exception as e:
            log.warning(f"Startup-failure cleanup: state store close failed: {e}")


def wire_fleet(
    device_manager: DeviceManager,
    mqtt_client: MQTTClient,
    wb_service: WBVirtualDeviceService,
    event_publisher: EventPublisherPort,
    dispatch_ring: DispatchRing,
    capabilities_dir: Path,
) -> None:
    """The post-``initialize_devices`` fleet wiring recipe — ONE copy for boot
    AND reload (CORE-15).

    Assigns every device its runtime collaborators (MQTT client, WB service,
    SSE event publisher, problem-report dispatch ring), attaches the Layer-1
    capability maps, and runs the command-exposure check. The reload path used
    to re-assign only ``mqtt_client``: the re-initialized fleet ran with no
    capability maps (canonical dispatch answered ``capability_not_supported``
    fleet-wide, the catalog published empty capabilities), no SSE publisher,
    and no dispatch ring — until the next container restart. Boot and reload
    now share this single recipe so the two compositions cannot drift.
    """
    logger = logging.getLogger(__name__)
    from locveil_bridge.infrastructure.devices.base import BaseDevice as _BaseDevice  # local: keep domain/ import-pure
    for device_id, port_device in device_manager.devices.items():
        device = cast(_BaseDevice, port_device)
        device.mqtt_client = mqtt_client
        device.wb_service = wb_service
        device.event_publisher = event_publisher  # SSE fan-out via EventPublisherPort
        device.dispatch_ring = dispatch_ring  # problem-report evidence (B-2)
        logger.info(f"Device {device_id} initialized with typed configuration and WB service")

    # Attach Layer 1 capability maps from config/capabilities/ (hot-fixable JSON).
    attach_capability_maps(device_manager.devices, capabilities_dir)
    logger.info("Attached capability maps to devices")

    exposure_violations = validate_command_exposure(device_manager.devices)
    if exposure_violations:
        logger.warning(
            "Command-exposure check: %d command(s) are `exposed` but not capability-backed "
            "(they will be invisible in Layer-3 manifests — mark `exposed: false` or add a "
            "capability): %s",
            len(exposure_violations), ", ".join(sorted(exposure_violations)),
        )
    else:
        logger.info(
            "Command-exposure check: OK (every device command is exposed:false or capability-backed)"
        )


def make_wb_cards_publisher(
    device_manager: DeviceManager,
    get_scenario_adapter: Callable[[], ScenarioWBAdapter],
) -> Callable[[], Any]:
    """One-shot publisher of the WB device cards + scenario cards (CORE-14).

    Returned coroutine function is registered as an MQTT on-connect callback AND
    called directly when the boot connect already happened — whichever runs first
    wins, the latch makes every later invocation a no-op. One-shot, unlike the
    VWB-32 catalog callback: card setup publishes config-derived initial control
    values, so re-running on a later reconnect would clobber live values with
    defaults. The latch is safe against the client loop's callback firing
    concurrently with the direct call — check-and-set with no await in between,
    single event loop. ``get_scenario_adapter`` is resolved at fire time so a
    /reload's adapter swap (nonlocal rebind) is honored.
    """
    published = False

    async def _publish_wb_cards_once() -> None:
        nonlocal published
        if published:
            return
        published = True
        from locveil_bridge.infrastructure.devices.base import BaseDevice as _BaseDevice  # local: keep domain/ import-pure
        logger = logging.getLogger(__name__)
        logger.info("Setting up Wirenboard virtual device emulation...")
        for device_id, port_device in device_manager.devices.items():
            device = cast(_BaseDevice, port_device)
            try:
                await device.setup_wb_emulation_if_enabled()
                logger.debug(f"WB emulation setup completed for device {device_id}")
            except Exception as e:
                logger.error(f"Failed to setup WB emulation for device {device_id}: {str(e)}")
        try:
            await get_scenario_adapter().setup()
        except Exception as e:
            logger.error(f"Failed to set up scenario WB cards: {str(e)}")

    return _publish_wb_cards_once


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    
    # Global instances
    config_manager = None
    device_manager = None
    mqtt_client = None
    state_store = None
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Lifespan context manager for FastAPI application."""
        # Startup
        nonlocal config_manager, device_manager, mqtt_client, state_store

        # Predeclared so the startup-failure cleanup below can reference it no
        # matter where the startup died (it is created near the end of startup).
        report_retry_task: asyncio.Task | None = None

        # OPS-8: the whole startup is wrapped — an unexpected failure anywhere in
        # here releases the already-acquired resources (best effort) and re-raises,
        # instead of leaking sockets/ports into a hung process.
        try:
            # Initialize config manager
            config_manager = ConfigManager()
        
            # Set app title from config
            service_name = config_manager.get_service_name()
            app.title = service_name
        
            # Setup logging with system config
            system_config = config_manager.get_system_config()
            log_file = system_config.log_file or 'logs/service.log'
            log_level = system_config.log_level
            setup_logging(log_file, log_level)
        
            # Check for log level override from environment
            override_log_level = os.getenv('OVERRIDE_LOG_LEVEL')
            if override_log_level:
                override_numeric_level = getattr(logging, override_log_level.upper(), None)
                if override_numeric_level is not None:
                    root_logger = logging.getLogger()
                    root_logger.setLevel(override_numeric_level)
                    print(f"Log level overridden by environment variable: {override_log_level}")
                else:
                    print(f"Warning: Invalid log level override '{override_log_level}', ignoring")
        
            # Apply logger-specific configuration
            if system_config.loggers:
                for logger_name, logger_level in system_config.loggers.items():
                    specific_logger = logging.getLogger(logger_name)
                    specific_level = getattr(logging, logger_level.upper(), logging.INFO)
                    specific_logger.setLevel(specific_level)
                    logging.info(f"Set logger {logger_name} to level {logger_level}")
        
            logger = logging.getLogger(__name__)
            logger.info("Starting MQTT Web Service")
        
            # Initialize state store after config but before device manager
            db_path = Path(system_config.persistence.db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            state_store = SQLiteStateStore(db_path=str(db_path))
            await state_store.initialize()
            logger.info(f"State persistence initialized with SQLite at {db_path}")
        
            # Initialize MQTT client first
            mqtt_broker_config = system_config.mqtt_broker
        
            # Check if maintenance is enabled and create guard if needed
            maintenance_guard = None
            if config_manager.is_maintenance_enabled():
                maintenance_config = config_manager.get_maintenance_config()
                assert maintenance_config is not None, "is_maintenance_enabled() implies non-None config"
                logger.info(f"Maintenance is enabled - creating WirenboardMaintenanceGuard with duration={maintenance_config.duration}s, topic={maintenance_config.topic}")
                maintenance_guard = WirenboardMaintenanceGuard(
                    duration=maintenance_config.duration,
                    topic=maintenance_config.topic
                )
            else:
                logger.info("Maintenance is disabled - no maintenance guard will be used")
            
            mqtt_client = MQTTClient({
                'host': mqtt_broker_config.host,
                'port': mqtt_broker_config.port,
                'client_id': mqtt_broker_config.client_id,
                'keepalive': mqtt_broker_config.keepalive,
                'auth': mqtt_broker_config.auth
            }, maintenance_guard=maintenance_guard)

            # Problem-report evidence rings (problem_reports_bridge.md B-2): always on,
            # in-memory, dumped only into report bundles / the evidence endpoint.
            reports_cfg = system_config.reports
            dispatch_ring = DispatchRing(depth=reports_cfg.dispatch_ring_depth)
            mqtt_window = MqttWindow(
                max_age_s=reports_cfg.mqtt_window_seconds,
                max_entries=reports_cfg.mqtt_window_max_messages,
            )
            mqtt_client.traffic_observer = mqtt_window.record
        
            # Initialize device manager with state repository
            device_manager = DeviceManager(state_repository=state_store)
            await device_manager.load_device_modules()
        
            # Log the number of typed configurations
            typed_configs = config_manager.get_all_typed_configs()
            if typed_configs:
                logger.info(f"Using {len(typed_configs)} typed device configurations")
        
            # Create WB virtual device service BEFORE initializing devices so device
            # constructors receive it -- WB-passthrough devices' setup() needs both
            # `mqtt_client` (to subscribe to state_topic + meta/error) and the service to
            # decide whether to register the WB virtual device (which they skip via
            # `enable_wb_emulation=False`).
            wb_service = WBVirtualDeviceService(message_bus=mqtt_client)
            logger.info("Created WB virtual device service")
            device_manager.set_runtime_services(mqtt_client=mqtt_client, wb_service=wb_service)

            # Initialize devices using typed configurations only
            await device_manager.initialize_devices(config_manager.get_all_device_configs())

            # Wire the fleet's runtime collaborators + capability maps via the
            # shared boot/reload recipe (CORE-15 — `wire_fleet`; the reload path
            # calls the same function so the two compositions cannot drift).
            # device_manager.devices values are DevicePort by contract -- at
            # runtime every shipped impl is BaseDevice; app/ is the composition
            # root, allowed to know infrastructure types.
            wire_fleet(
                device_manager, mqtt_client, wb_service,
                sse_manager, dispatch_ring,
                Path(config_manager.config_dir) / "capabilities",
            )

            # Persisted device state is restored inside initialize_devices(), per device BEFORE
            # its setup() — it must precede the post-setup initial persist, which would otherwise
            # clobber the last-good snapshot with boot defaults. (Replaces the old
            # device_manager.initialize() restore stub that ran here, too late.)

            # Get topics for all devices
            device_topics: dict[str, list[str]] = {}
            for device_id, device in device_manager.devices.items():
                topics = device.subscribe_topics()
                device_topics[device_id] = topics
                logger.info(f"Device {device_id} subscribed to topics: {topics}")

            # Connect MQTT client. Filter out any None handlers
            # (get_message_handler returns Optional; skip the missing ones rather
            # than passing them down).
            handler_map: dict[str, Callable[..., Any]] = {}
            for device_id, topics in device_topics.items():
                handler = device_manager.get_message_handler(device_id)
                if handler is None:
                    continue
                for topic in topics:
                    handler_map[topic] = handler
            await mqtt_client.connect_and_subscribe(handler_map)
        
            # Wait for MQTT so the normal boot publishes WB cards before uvicorn
            # serves. A timeout is no longer terminal (CORE-14): card publishing
            # lives in a one-shot on-connect callback registered below, so a boot
            # that loses the race to the host network (power-outage cold boot: the
            # broker's LAN IP doesn't exist until the interface is up) publishes
            # on whichever connect attempt eventually lands.
            logger.info("Waiting for MQTT connection to be established...")
            connection_success = await mqtt_client.wait_for_connection(timeout=30.0)
            if connection_success:
                logger.info("MQTT connection established successfully")
            else:
                logger.warning(
                    "MQTT connection not established within timeout - WB virtual cards "
                    "will be published on first connect"
                )

            # Initialize room manager (after devices are loaded)
            room_manager = RoomManager(Path(config_manager.config_dir), device_manager)
        
            # Initialize scenario manager
            scenario_manager = ScenarioManager(
                device_manager=device_manager,
                room_manager=room_manager,
                state_repository=state_store,
                scenario_dir=Path(config_manager.config_dir) / "scenarios"
            )
            await scenario_manager.initialize()
            logger.info("Scenario manager initialized")

            # Scenario <-> Wirenboard integration (SCN-6, canonical_first.md §3-§4): one
            # Scenario Manager entity per scenario-bearing room. The domain proxy resolves
            # role -> device at fire time for REST/UI/WB alike; the WB adapter renders each
            # entity as a «Сценарии» card and keeps its value topic tracking the room slot.
            scenario_proxy = ScenarioProxy(scenario_manager, device_manager)
            scenario_wb_adapter = ScenarioWBAdapter(scenario_proxy, wb_service, mqtt_client)

            # CORE-14: WB device cards + scenario cards publish from a ONE-SHOT
            # on-connect callback instead of a connect-or-skip gate (see
            # make_wb_cards_publisher). The adapter is resolved lazily so a
            # /reload's rebind of `scenario_wb_adapter` is honored.
            publish_wb_cards_once = make_wb_cards_publisher(
                device_manager, lambda: scenario_wb_adapter
            )
            mqtt_client.on_connect_callbacks.append(publish_wb_cards_once)
            # Direct call for the boot that won the race (the first connect
            # already fired its callbacks before this registration). Gate on the
            # LIVE flag, not the `connection_success` snapshot — the connect may
            # have landed between the wait timing out and this line.
            if mqtt_client.connected:
                await publish_wb_cards_once()

            # SSE observer on the activation chokepoint: EVERY path (REST, canonical
            # scenario.set, restore, deactivate) notifies the scenarios channel, so an
            # open scenario page goes live regardless of who switched. (Rack finding
            # 2026-07-07: the canonical path — the UI's PRIMARY path since UI-9 —
            # emitted nothing; only the legacy REST routers did, and the page stayed
            # stale until reload.)
            async def _scenario_sse_observer(room_id: str) -> None:
                active = scenario_manager.active.get(room_id)
                payload: Dict[str, Any] = {
                    "scenario_id": active.scenario_id if active else None,
                    "room_id": room_id,
                    "timestamp": datetime.now().isoformat(),
                }
                if active is not None:
                    payload["state"] = scenario_manager.get_scenario_state(active.scenario_id).model_dump()
                await sse_manager.broadcast(
                    channel=SSEChannel.SCENARIOS,
                    event_type="scenario_switched" if active else "scenario_shutdown",
                    data=payload,
                )

            scenario_manager.active_changed_observers.append(_scenario_sse_observer)

            # Problem-report service (problem_reports_bridge.md): the collector behind
            # POST /reports (filing, opt-in) and GET /reports/evidence (B-11, always on).
            # Cross-layer inputs go in as callables so the domain service stays import-pure.
            import platform as _platform
            assert state_store is not None  # set earlier in this lifespan; narrows for the closure below
            _report_state_store = state_store
            report_sink = None
            if reports_cfg.enabled:
                assert reports_cfg.repo is not None  # ReportsConfig validator: enabled requires repo
                report_sink = GitHubReportSink(
                    repo=reports_cfg.repo,
                    token_env=reports_cfg.token_env,
                    spool_dir=Path("data/reports"),
                )
            report_service = ReportService(
                settings=ReportsSettings(
                    enabled=reports_cfg.enabled,
                    max_reports_per_hour=reports_cfg.max_reports_per_hour,
                    max_reports_per_day=reports_cfg.max_reports_per_day,
                    log_file=Path(system_config.log_file) if system_config.log_file else None,
                ),
                device_manager=device_manager,
                scenario_manager=scenario_manager,
                sink=report_sink,
                dispatch_ring=dispatch_ring,
                mqtt_window=mqtt_window,
                persisted_state=lambda did: _report_state_store.get(f"device:{did}"),
                system_config=lambda: system_config.model_dump(mode="json"),
                catalog_version=lambda: build_catalog(device_manager, room_manager, scenario_proxy).version,
                bridge_version=__version__,
                platform=f"{_platform.system()}-{_platform.machine()}",
            )

            # B-7 spool retry: once at startup, then hourly (only meaningful when filing
            # is enabled — a disabled bridge never spools). (report_retry_task is
            # predeclared above the try so the failure cleanup can reference it.)
            if report_sink is not None:
                _retry_sink = report_sink
                async def _report_retry_loop() -> None:
                    while True:
                        try:
                            delivered = await _retry_sink.retry_spooled()
                            if delivered:
                                logger.info(f"Delivered {delivered} spooled problem report(s)")
                        except Exception as e:
                            logger.error(f"Spooled-report retry failed: {str(e)}")
                        await asyncio.sleep(3600)
                report_retry_task = asyncio.create_task(_report_retry_loop())

            # Initialize routers with dependencies
            system.initialize(config_manager, device_manager, mqtt_client, state_store, scenario_manager, room_manager, scenario_proxy)
            devices.initialize(config_manager, device_manager, mqtt_client, scenario_proxy)
            mqtt.initialize(mqtt_client)
            scenarios.initialize(scenario_manager, room_manager, mqtt_client)
            rooms.initialize(room_manager, device_manager)  # device_manager: VWB-23 group dispatch
            state.initialize(config_manager, device_manager, state_store, scenario_manager)
            events.initialize()  # Initialize SSE events router
            reports.initialize(report_service)

            # VWB-32: publish the retained catalog version at STARTUP and on every MQTT
            # (re)connect — previously it was published only from POST /reload, so a
            # broker restart (the WB7 runs mosquitto WITHOUT persistence — every restart
            # wipes ALL retained messages) left `bridge/catalog/version` missing until
            # the next reload, blinding the voice side's catalog-staleness gate. The
            # on-connect callback makes the bridge self-healing mid-run; the immediate
            # call covers this boot (the first connect already happened above).
            async def _publish_catalog_version() -> None:
                if mqtt_client is None:
                    return
                try:
                    catalog = build_catalog(device_manager, room_manager, scenario_proxy)
                    await mqtt_client.publish(
                        system.CATALOG_VERSION_TOPIC, catalog.version, retain=True
                    )
                    logger.info(
                        f"Published catalog version {catalog.version!r} to "
                        f"{system.CATALOG_VERSION_TOPIC} (retained)"
                    )
                except Exception as e:  # never let the nudge break startup/reconnect
                    logger.warning(f"Failed to publish retained catalog version: {e}")

            mqtt_client.on_connect_callbacks.append(_publish_catalog_version)
            await _publish_catalog_version()

            # CORE-1: the /reload background work is an app-layer service now;
            # the system router only schedules it. Composition stays here:
            # the factory builds the replacement client exactly the way this
            # startup built the original (maintenance guard + traffic
            # observer — the old inline reload silently lost both), and the
            # adopt hook re-points every router global, re-registers the
            # on-connect catalog publish (VWB-32), updates the shutdown
            # reference, and hands back a fresh WB service bound to the new
            # client (the old one kept publishing into the stopped client).
            # Bound here (post-assignment) so the closures see the non-None
            # manager without re-reading the nonlocal.
            _reload_cfg_manager = config_manager

            def _build_mqtt_client() -> MQTTClient:
                broker = _reload_cfg_manager.get_mqtt_broker_config()
                client = MQTTClient({
                    'host': broker.host,
                    'port': broker.port,
                    'client_id': broker.client_id,
                    'keepalive': broker.keepalive,
                    'auth': broker.auth
                }, maintenance_guard=maintenance_guard)
                client.traffic_observer = mqtt_window.record
                return client

            def _adopt_mqtt_client(new_client: MQTTClient) -> WBVirtualDeviceService:
                nonlocal mqtt_client
                mqtt_client = new_client
                system.initialize(config_manager, device_manager, mqtt_client, state_store, scenario_manager, room_manager, scenario_proxy)
                devices.initialize(config_manager, device_manager, mqtt_client, scenario_proxy)
                mqtt.initialize(mqtt_client)
                scenarios.initialize(scenario_manager, room_manager, mqtt_client)
                new_client.on_connect_callbacks.append(_publish_catalog_version)
                return WBVirtualDeviceService(message_bus=new_client)

            async def _rebuild_scenario_cards(
                new_client: MQTTClient, new_wb_service: WBVirtualDeviceService
            ) -> None:
                # A fresh adapter over the new client: setup() republishes the
                # cards, re-subscribes their command topics, and re-points the
                # scenario manager's on_active_changed hook at itself — the old
                # adapter (cards, subscriptions, hook) died with the old client.
                nonlocal scenario_wb_adapter
                scenario_wb_adapter = ScenarioWBAdapter(scenario_proxy, new_wb_service, new_client)
                try:
                    await scenario_wb_adapter.setup()
                except Exception as e:
                    logger.error(f"Failed to re-set up scenario WB cards after reload: {str(e)}")

            # Narrowed bindings for the closure below — the nonlocals are
            # Optional until startup assigns them, which has happened by here.
            reload_device_manager = device_manager
            reload_capabilities_dir = Path(config_manager.config_dir) / "capabilities"

            async def _rewire_fleet_after_reload(
                new_client: MQTTClient, new_wb_service: WBVirtualDeviceService
            ) -> None:
                # CORE-15: full config-bundle parity on /reload. The shared
                # boot recipe first (runtime collaborators + capability maps +
                # exposure check — without it the re-initialized fleet ran
                # capability-less until the next restart), then the config
                # consumers boot constructs AFTER the fleet: room definitions +
                # membership over the new device objects, scenario definitions +
                # the Layer-0 topology. Scenario restore is tracking-only
                # (SCN-18) — safe to re-run against a live house.
                wire_fleet(
                    reload_device_manager, new_client, new_wb_service,
                    sse_manager, dispatch_ring, reload_capabilities_dir,
                )
                room_manager.reload()
                await scenario_manager.initialize()

            reload_service = ReloadService(
                config_manager=config_manager,
                device_manager=device_manager,
                client_factory=_build_mqtt_client,
                on_new_client=_adopt_mqtt_client,
                rewire_fleet=_rewire_fleet_after_reload,
                rebuild_scenario_cards=_rebuild_scenario_cards,
                publish_catalog_version=_publish_catalog_version,
            )
            reload_service.mqtt_client = mqtt_client
            system.set_reload_service(reload_service)

            logger.info("System startup complete")

        except Exception:
            # OPS-8 defensive startup-failure cleanup: the handled cases (device
            # setup failures, scenario load errors) never reach here — this is the
            # net for the UNEXPECTED failure that would otherwise leak an open
            # SQLite handle / connected MQTT client / device sockets into a hung
            # process. Best-effort release, then re-raise so the process exits
            # with the real error.
            logging.getLogger(__name__).exception(
                "Startup failed — releasing partially initialized resources"
            )
            await _release_partial_startup(
                report_retry_task, device_manager, mqtt_client, state_store
            )
            raise

        yield  # Service is running
        
        # Shutdown
        logger.info("System shutting down...")
        
        try:
            # Stop the problem-report spool retry loop (pure timer; nothing to flush).
            if report_retry_task is not None:
                report_retry_task.cancel()

            # Shutdown SSE connections first to prevent blocking
            logger.info("Shutting down SSE connections...")
            await sse_manager.shutdown()

            # NOTE: do NOT blanket-cancel asyncio.all_tasks() here. We run inside uvicorn's
            # lifespan task, and all_tasks() also contains uvicorn's own serve task — which is
            # parked in `lifespan.shutdown()` awaiting our completion. Cancelling it tears the
            # serve task out from under us (CancelledError out the top of asyncio.run) and
            # prematurely kills the MQTT task before the ordered disconnect below. The ordered
            # teardown that follows stops every task we own; asyncio.run() cancels any stragglers
            # after the lifespan returns cleanly. See action_plan.md §5.1 #8.

            # Flush in-flight (real) state writes BEFORE marking shutdown, so the last operating
            # state is persisted. After this, the persistence callback stops saving — device
            # teardown mutates state to disconnected/off, which must NOT overwrite the assumed state.
            logger.info("Flushing pending persistence before shutdown...")
            try:
                await device_manager.wait_for_persistence_tasks(timeout=2.0)
            except asyncio.CancelledError:
                logger.warning("Persistence flush interrupted by cancellation")

            # Prepare the device manager for shutdown (stops further persistence; teardown is
            # transparent to the hardware — the active scenario stays on the devices).
            logger.info("Preparing device manager for shutdown...")
            await device_manager.prepare_for_shutdown()

            # Shutdown scenario manager
            logger.info("Shutting down scenario manager...")
            await scenario_manager.shutdown()
            
            # Shutdown room manager
            logger.info("Shutting down room manager...")
            await room_manager.shutdown()

            # Mark regular-device WB virtual cards offline (meta/error=offline +
            # meta/available=0) while MQTT is still connected — scenario cards are
            # handled by scenario_manager.shutdown() above, but device cards used
            # to keep their retained available=1 forever, looking live in the WB
            # UI with the bridge down (OPS-8). Hardware-transparent: this touches
            # broker metadata only, never the devices.
            logger.info("Marking WB virtual device cards offline...")
            from locveil_bridge.infrastructure.devices.base import BaseDevice as _BaseDevice  # local: keep domain/ import-pure
            for port_device in device_manager.devices.values():
                wb_dev = cast(_BaseDevice, port_device)
                try:
                    await wb_dev.cleanup_wb_device_state()
                except Exception as e:
                    logger.warning(
                        f"WB offline cleanup failed for {wb_dev.device_id}: {e}"
                    )

            # Disconnect MQTT to prevent incoming messages during shutdown
            logger.info("Disconnecting MQTT client...")
            await mqtt_client.disconnect()
            
            # Shutdown devices
            logger.info("Shutting down devices...")
            await device_manager.shutdown_devices()
            
            # No post-teardown persistence: device teardown mutates state to disconnected/off,
            # which must NOT overwrite the assumed state (already flushed above, pre-teardown).
            # This keeps a bridge restart transparent to the hardware.

            # Close state store after all persistence is done
            logger.info("Closing state persistence connection...")
            try:
                await state_store.close()
            except asyncio.CancelledError:
                logger.warning("State store close interrupted by cancellation")
            
            logger.info("System shutdown complete")
            
        except asyncio.CancelledError:
            logger.warning("Shutdown sequence interrupted by cancellation - performing emergency cleanup")
            
            # Emergency cleanup - fire and forget critical operations
            try:
                # Try to close state store without waiting
                await asyncio.wait_for(state_store.close(), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as e:
                logger.warning(f"Emergency state store close failed: {e}")
            
            # Re-raise the cancellation to let uvicorn handle it properly
            raise

    # Create the FastAPI app with lifespan
    app = FastAPI(
        title="MQTT Web Service",
        description="A web service that manages MQTT devices with typed configurations",
        version=__version__,
        lifespan=lifespan
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # For local network, allow all origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(system.router)
    app.include_router(devices.router)
    app.include_router(mqtt.router)
    app.include_router(scenarios.router)
    app.include_router(rooms.router)
    app.include_router(state.router)
    app.include_router(events.router)
    app.include_router(reports.router)

    _install_openapi_with_state_models(app)

    return app


# Device-state models that must be present in /openapi.json so the UI's
# build-time codegen can read state shapes from the API contract instead of
# importing the Python package and AST-parsing these classes (action_plan P1 #3.5).
# These are runtime-typed states returned by /devices/{id}/state and persisted by
# the state store; the codegen maps each by class name via device-state-mapping.json.
OPENAPI_EXTRA_MODELS = [
    BaseDeviceState,
    KitchenHoodState,
    LgTvState,
    WirenboardIRState,
    RevoxA77ReelToReelState,
    AppleTVState,
    AuralicDeviceState,
    EmotivaXMC2State,
    MitsubishiHvacState,
]


def _install_openapi_with_state_models(app: FastAPI) -> None:
    """Override app.openapi() to inject device-state model schemas.

    These models are not the response_model of any operation (the live endpoints
    return plain dicts / instances and keep their custom serialization), so they
    would not otherwise appear in components.schemas. We add them as standalone
    schemas — purely additive, no endpoint behavior changes — so both
    openapi-typescript (api.gen.ts) and the device-page StateTypeGenerator can
    consume them from the contract.
    """

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        schemas = openapi_schema.setdefault("components", {}).setdefault("schemas", {})
        for model in OPENAPI_EXTRA_MODELS:
            model_schema = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            # Pydantic emits nested/referenced models under "$defs"; lift them to
            # components.schemas so the $ref pointers resolve.
            for dep_name, dep_schema in model_schema.pop("$defs", {}).items():
                schemas.setdefault(dep_name, dep_schema)
            schemas[model.__name__] = model_schema

        app.openapi_schema = openapi_schema
        return openapi_schema

    app.openapi = custom_openapi 