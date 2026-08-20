/**
 * Tipagens e API para Live Activities (Dynamic Island).
 * Uso em bundlers futuros; na Web/Capacitor atual o runtime é
 * `static/js/dynamicIslandService.js` (window.MeninoAlhoDynamicIsland).
 */

export interface StartDeliveryActivityOptions {
  numeroCarga?: string;
  placaVeiculo?: string;
  proximoCliente: string;
  totalCaixas: number;
  caixasEntregues?: number;
  status?: string;
}

export interface UpdateDeliveryActivityOptions {
  caixasEntregues?: number;
  totalCaixas?: number;
  proximoCliente?: string;
  status?: string;
}

export interface StopDeliveryActivityOptions {
  status?: string;
  caixasEntregues?: number;
  totalCaixas?: number;
  dismissalSeconds?: number;
}

export interface DeliveryActivityResult {
  started?: boolean;
  updated?: boolean;
  stopped?: boolean;
  skipped?: boolean;
  reason?: string;
  activityId?: string;
  progresso?: number;
  count?: number;
}

type NativePlugin = {
  start(options: Record<string, unknown>): Promise<DeliveryActivityResult>;
  update(options: Record<string, unknown>): Promise<DeliveryActivityResult>;
  stop(options: Record<string, unknown>): Promise<DeliveryActivityResult>;
};

declare global {
  interface Window {
    Capacitor?: {
      isNativePlatform?: () => boolean;
      getPlatform?: () => string;
      Plugins?: { DeliveryActivity?: NativePlugin };
      registerPlugin?: (name: string) => NativePlugin;
    };
    MeninoAlhoDynamicIsland?: {
      isAvailable: () => boolean;
      startDeliveryActivity: (o: StartDeliveryActivityOptions) => Promise<DeliveryActivityResult>;
      updateDeliveryActivity: (o: UpdateDeliveryActivityOptions) => Promise<DeliveryActivityResult>;
      stopDeliveryActivity: (o?: StopDeliveryActivityOptions) => Promise<DeliveryActivityResult>;
    };
  }
}

function isCapacitorIOS(): boolean {
  try {
    const Cap = window.Capacitor;
    if (!Cap?.isNativePlatform?.()) return false;
    return Cap.getPlatform?.() === 'ios';
  } catch {
    return false;
  }
}

function getPlugin(): NativePlugin | null {
  const Cap = window.Capacitor;
  if (!Cap) return null;
  if (Cap.Plugins?.DeliveryActivity) return Cap.Plugins.DeliveryActivity;
  if (typeof Cap.registerPlugin === 'function') {
    return Cap.registerPlugin('DeliveryActivity');
  }
  return null;
}

function calcProgresso(entregues: number, total: number): number {
  if (!total || total <= 0) return 0;
  return Math.min(entregues / total, 1);
}

export async function startDeliveryActivity(
  options: StartDeliveryActivityOptions
): Promise<DeliveryActivityResult> {
  if (window.MeninoAlhoDynamicIsland?.startDeliveryActivity) {
    return window.MeninoAlhoDynamicIsland.startDeliveryActivity(options);
  }
  if (!isCapacitorIOS()) {
    return { started: false, skipped: true, reason: 'not_ios' };
  }
  const plugin = getPlugin();
  if (!plugin) return { started: false, skipped: true, reason: 'no_plugin' };

  const totalCaixas = Number(options.totalCaixas || 0);
  const caixasEntregues = Number(options.caixasEntregues || 0);
  return plugin.start({
    ...options,
    progresso: calcProgresso(caixasEntregues, totalCaixas),
  });
}

export async function updateDeliveryActivity(
  options: UpdateDeliveryActivityOptions
): Promise<DeliveryActivityResult> {
  if (window.MeninoAlhoDynamicIsland?.updateDeliveryActivity) {
    return window.MeninoAlhoDynamicIsland.updateDeliveryActivity(options);
  }
  if (!isCapacitorIOS()) {
    return { updated: false, skipped: true, reason: 'not_ios' };
  }
  const plugin = getPlugin();
  if (!plugin) return { updated: false, skipped: true, reason: 'no_plugin' };
  return plugin.update(options as Record<string, unknown>);
}

export async function stopDeliveryActivity(
  options: StopDeliveryActivityOptions = {}
): Promise<DeliveryActivityResult> {
  if (window.MeninoAlhoDynamicIsland?.stopDeliveryActivity) {
    return window.MeninoAlhoDynamicIsland.stopDeliveryActivity(options);
  }
  if (!isCapacitorIOS()) {
    return { stopped: false, skipped: true, reason: 'not_ios' };
  }
  const plugin = getPlugin();
  if (!plugin) return { stopped: false, skipped: true, reason: 'no_plugin' };
  return plugin.stop(options as Record<string, unknown>);
}
