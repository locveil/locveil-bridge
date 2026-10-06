// Layer 3: renders a scenario page at RUNTIME from GET /scenario/{id}/layout via the generic
// RemoteControlLayout. SCN-6 (canonical-first phase 1): every control dispatches through the
// room's Scenario Manager entity (manifest.canonicalEntityId) as a canonical command — the power
// zone becomes scenario.set/off, inherited controls carry their canonical (capability, action)
// tuple and the BRIDGE resolves role -> device at fire time (no stale-manifest targeting).
// Controls without a canonical annotation (e.g. list queries) fall back to the legacy
// per-device /action dispatch via sourceDeviceId.
import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useLogStore } from '../stores/useLogStore';
import { useExecuteCanonicalAction, useExecuteDeviceAction, useScenarioLayout, useScenarioState, useSwitchScenario, useShutdownScenario } from '../hooks/useApi';
import { useSettingsStore } from '../stores/useSettingsStore';
import { useRoomStore } from '../stores/useRoomStore';
import { RemoteControlLayout } from './RemoteControlLayout';
import { ForceReconcileDialog } from './ForceReconcileDialog';
import { manifestToDeviceStructure } from '../lib/layoutManifestAdapter';

/** The 409 `job_in_progress` of either door — canonical (`detail.error.job_id`) or REST
 *  (`detail.job_id`) — or null for any other failure. */
function describeJobInProgress(error: unknown): { jobId: string; code: 'job_in_progress' } | null {
  const detail = (error as { response?: { status?: number; data?: { detail?: unknown } } })?.response;
  if (detail?.status !== 409) return null;
  const body = detail.data?.detail as { code?: string; job_id?: string; error?: { code?: string; job_id?: string } } | undefined;
  if (body?.error?.code === 'job_in_progress' && body.error.job_id) return { jobId: body.error.job_id, code: 'job_in_progress' };
  if (body?.code === 'job_in_progress' && body.job_id) return { jobId: body.job_id, code: 'job_in_progress' };
  return null;
}

export function RuntimeScenarioPage({ scenarioId }: { scenarioId: string }) {
  const { addLog } = useLogStore();
  const queryClient = useQueryClient();
  const executeAction = useExecuteDeviceAction();
  const executeCanonical = useExecuteCanonicalAction();
  // Power-on routes through /scenario/switch (not /scenario/start) so it handles both cold-start
  // (no current scenario → outgoing=None branch) AND hand-off from an already-active scenario
  // via the reconciler delta path. /scenario/start 409s when anything is active.
  const switchScenario = useSwitchScenario();
  const shutdownScenario = useShutdownScenario();
  const { statePanelOpen } = useSettingsStore();
  const { selectScenario } = useRoomStore();
  const { data: manifest, isLoading, isError } = useScenarioLayout(scenarioId);
  // There is one global active scenario (ScenarioManager.current_scenario), so this scenario is
  // "running" iff it is the active one. /scenario/state 404s when nothing is active (query errors,
  // data undefined → not running). Kept live by the /events/scenarios → cache invalidation in
  // Layout.tsx. See ui_backend_contract.md "Scenario lifecycle (power zone) active-state".
  const { data: activeScenarioState } = useScenarioState();
  const lifecycleActive = activeScenarioState?.scenario_id === scenarioId;
  // SCN-11: the per-device force-reconcile dialog — active scenario only (the desired
  // state is defined by the RUNNING scenario; on an inactive page the gesture is "start").
  const [reconcileOpen, setReconcileOpen] = useState(false);

  useEffect(() => {
    selectScenario(scenarioId);
  }, [scenarioId, selectScenario]);

  const deviceStructure = useMemo(
    () => (manifest ? manifestToDeviceStructure(manifest) : null),
    [manifest]
  );

  // SCN-6: canonical dispatch table, built from the raw manifest. Key
  // `${sourceDeviceId}:${actionName}` -> { capability, action }. A generic walk keeps
  // this robust to zone-content shapes (any object carrying actionName + the canonical
  // annotation is a dispatchable control).
  const canonicalEntityId = (manifest as any)?.canonicalEntityId as string | undefined;
  const canonicalByAction = useMemo(() => {
    const map = new Map<string, { capability: string; action: string }>();
    const walk = (node: any) => {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) { node.forEach(walk); return; }
      if (node.actionName && node.canonicalCapability && node.canonicalAction && node.sourceDeviceId) {
        map.set(`${node.sourceDeviceId}:${node.actionName}`, {
          capability: node.canonicalCapability,
          action: node.canonicalAction,
        });
      }
      Object.values(node).forEach(walk);
    };
    walk(manifest?.remoteZones);
    return map;
  }, [manifest]);

  // Scenario jobs (contract v1.12): one job per room — a second lifecycle request while
  // one runs is refused with 409 `job_in_progress` (before this the second call silently
  // double-ran the chain). Say so, and re-read the room: the running job's terminal event
  // lands on /events/scenarios and the state queries refresh from there.
  const onScenarioJobRefused = (error: unknown) => {
    const refused = describeJobInProgress(error);
    if (!refused) return;
    addLog({ level: 'warn', message: `A scenario switch is already running in this room (${refused.jobId}) — wait for it to finish`, details: refused });
    void queryClient.invalidateQueries({ queryKey: ['scenario', 'state'] });
    void queryClient.invalidateQueries({ queryKey: ['scenarios', 'state'] });
  };

  const handleAction = (action: string, payload?: any, targetDeviceId?: string) => {
    const params =
      payload === undefined || payload === null || (Array.isArray(payload) && payload.length === 0)
        ? {}
        : payload;

    // The scenario power zone drives the lifecycle. Canonical-first (SCN-6): the room's
    // Scenario Manager entity handles scenario.set / scenario.off; the legacy
    // /scenario/switch + /scenario/shutdown mutations remain as fallback for manifests
    // without a canonical entity (older bridge).
    if (action === 'power_on') {
      if (canonicalEntityId) {
        executeCanonical.mutate(
          { deviceId: canonicalEntityId, request: { capability: 'scenario', action: 'set', params: { value: scenarioId }, wait: true } },
          { onError: onScenarioJobRefused },
        );
      } else {
        switchScenario.mutate({ id: scenarioId, graceful: true, wait: true }, { onError: onScenarioJobRefused });
      }
      addLog({ level: 'info', message: `Starting scenario: ${scenarioId}`, details: params });
      return;
    }
    if (action === 'power_off') {
      if (canonicalEntityId) {
        executeCanonical.mutate(
          { deviceId: canonicalEntityId, request: { capability: 'scenario', action: 'off', wait: true } },
          { onError: onScenarioJobRefused },
        );
      } else {
        shutdownScenario.mutate({ scenarioId, graceful: true }, { onError: onScenarioJobRefused });
      }
      addLog({ level: 'info', message: `Shutting down scenario: ${scenarioId}`, details: params });
      return;
    }

    // Inherited controls: canonical dispatch through the Scenario Manager entity when the
    // manifest annotated the control (the bridge resolves role -> device at fire time; the
    // response's executed_on names the real target). Params pass through by name — the
    // canonical endpoint renames only names present in the capability's param_map, so the
    // manifest's native param names arrive unchanged at the handler.
    const canonical = targetDeviceId ? canonicalByAction.get(`${targetDeviceId}:${action}`) : undefined;
    if (canonical && canonicalEntityId) {
      // wait:false (SCN-7): fire-and-return — button presses must not serialize on echo waits.
      executeCanonical.mutate({ deviceId: canonicalEntityId, request: { capability: canonical.capability, action: canonical.action, params, wait: false } });
      addLog({ level: 'info', message: `Canonical: ${canonical.capability}.${canonical.action} -> ${canonicalEntityId}`, details: params });
      return;
    }

    // Fallback (un-annotated controls, e.g. list queries): route to the ROLE DEVICE
    // (sourceDeviceId). A scenario is not a device, so we must never post to
    // /devices/{scenario}; if a control lacks a role device, that's a manifest bug —
    // warn and skip rather than hit a non-existent endpoint.
    if (!targetDeviceId) {
      console.warn(`[scenario ${scenarioId}] control "${action}" has no role device (sourceDeviceId) — skipping`);
      addLog({ level: 'error', message: `Scenario control "${action}" has no role device`, details: params });
      return;
    }
    executeAction.mutate({ deviceId: targetDeviceId, action: { action, params } });
    addLog({ level: 'info', message: `Action: ${action} -> ${targetDeviceId}`, details: params });
  };

  if (isLoading) {
    return <div className="p-6 text-center text-muted-foreground">Loading layout…</div>;
  }

  if (isError || !deviceStructure) {
    return (
      <div className="p-6 text-center">
        <h1 className="text-2xl font-bold mb-4">Layout unavailable</h1>
        <p className="text-muted-foreground">
          Could not load the runtime layout for scenario "{scenarioId}".
        </p>
      </div>
    );
  }

  return (
    <div className={`${statePanelOpen ? 'p-2' : 'p-4'}`}>
      <RemoteControlLayout
        deviceStructure={deviceStructure}
        onAction={handleAction}
        isActionPending={executeAction.isPending || executeCanonical.isPending || switchScenario.isPending || shutdownScenario.isPending}
        actionError={executeAction.error || executeCanonical.error || switchScenario.error || shutdownScenario.error}
        lastAction={
          switchScenario.isPending || (executeCanonical.isPending && executeCanonical.variables?.request.capability === 'scenario' && executeCanonical.variables?.request.action === 'set') ? 'power_on'
          : shutdownScenario.isPending || (executeCanonical.isPending && executeCanonical.variables?.request.capability === 'scenario' && executeCanonical.variables?.request.action === 'off') ? 'power_off'
          : executeAction.variables?.action.action
        }
        className="w-full"
        lifecycleActive={lifecycleActive}
        // Transition-aware reconciler notes (§5.1 #1) — only when THIS scenario is the
        // active one (viewing an inactive scenario's page must NOT show another
        // scenario's prompts).
        manualSteps={lifecycleActive ? activeScenarioState?.manual_steps ?? [] : []}
      />
      {lifecycleActive && (
        <div className="mt-3 flex justify-center">
          <button
            className="px-3 py-1.5 text-xs rounded-md border border-border text-muted-foreground hover:bg-accent"
            onClick={() => setReconcileOpen(true)}
          >
            ⚡ Device states…
          </button>
        </div>
      )}
      <ForceReconcileDialog
        scenarioId={scenarioId}
        open={reconcileOpen}
        onClose={() => setReconcileOpen(false)}
      />
    </div>
  );
}

export default RuntimeScenarioPage;
