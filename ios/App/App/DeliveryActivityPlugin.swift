import Foundation
import ActivityKit
import Capacitor

@objc(DeliveryActivityPlugin)
public class DeliveryActivityPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "DeliveryActivityPlugin"
    public let jsName = "DeliveryActivity"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "startDeliveryActivity", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "updateDeliveryActivity", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "endDeliveryActivity", returnType: CAPPluginReturnPromise),
        // Aliases curtos usados por helpers legados
        CAPPluginMethod(name: "start", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "update", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop", returnType: CAPPluginReturnPromise)
    ]

    @objc func startDeliveryActivity(_ call: CAPPluginCall) {
        startInternal(call)
    }

    @objc func start(_ call: CAPPluginCall) {
        startInternal(call)
    }

    @objc func updateDeliveryActivity(_ call: CAPPluginCall) {
        updateInternal(call)
    }

    @objc func update(_ call: CAPPluginCall) {
        updateInternal(call)
    }

    @objc func endDeliveryActivity(_ call: CAPPluginCall) {
        endInternal(call)
    }

    @objc func stop(_ call: CAPPluginCall) {
        endInternal(call)
    }

    private func startInternal(_ call: CAPPluginCall) {
        guard #available(iOS 16.1, *) else {
            call.reject("Live Activities exigem iOS 16.1 ou superior.")
            return
        }

        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            call.reject("Live Activities desativadas nas configurações do iPhone.")
            return
        }

        let numeroCarga = call.getString("numeroCarga") ?? "—"
        let proximoCliente = call.getString("proximoCliente") ?? "—"
        let totalCaixas = call.getInt("totalCaixas") ?? 0
        let caixasEntregues = call.getInt("caixasEntregues") ?? 0
        let status = call.getString("status") ?? "Em rota"

        let attributes = DeliveryActivityAttributes(numeroCarga: numeroCarga)
        let state = DeliveryActivityAttributes.ContentState(
            caixasEntregues: caixasEntregues,
            totalCaixas: totalCaixas,
            proximoCliente: proximoCliente,
            status: status
        )

        do {
            for activity in Activity<DeliveryActivityAttributes>.activities {
                Task { await activity.end(nil, dismissalPolicy: .immediate) }
            }

            let content = ActivityContent(state: state, staleDate: nil)
            let activity = try Activity.request(
                attributes: attributes,
                content: content,
                pushType: nil
            )
            call.resolve([
                "activityId": activity.id,
                "started": true
            ])
        } catch {
            call.reject("Falha ao iniciar Live Activity: \(error.localizedDescription)")
        }
    }

    private func updateInternal(_ call: CAPPluginCall) {
        guard #available(iOS 16.1, *) else {
            call.reject("Live Activities exigem iOS 16.1 ou superior.")
            return
        }

        let activities = Activity<DeliveryActivityAttributes>.activities
        guard let activity = activities.first else {
            call.reject("Nenhuma Live Activity ativa para atualizar.")
            return
        }

        let current = activity.content.state
        let state = DeliveryActivityAttributes.ContentState(
            caixasEntregues: call.getInt("caixasEntregues") ?? current.caixasEntregues,
            totalCaixas: call.getInt("totalCaixas") ?? current.totalCaixas,
            proximoCliente: call.getString("proximoCliente") ?? current.proximoCliente,
            status: call.getString("status") ?? current.status
        )

        Task {
            await activity.update(ActivityContent(state: state, staleDate: nil))
            call.resolve([
                "activityId": activity.id,
                "updated": true
            ])
        }
    }

    private func endInternal(_ call: CAPPluginCall) {
        guard #available(iOS 16.1, *) else {
            call.reject("Live Activities exigem iOS 16.1 ou superior.")
            return
        }

        let activities = Activity<DeliveryActivityAttributes>.activities
        guard !activities.isEmpty else {
            call.resolve(["stopped": true, "ended": true, "count": 0])
            return
        }

        let statusFinal = call.getString("status") ?? "Concluído"
        let dismissalSeconds = call.getDouble("dismissalSeconds") ?? 5

        Task {
            for activity in activities {
                let current = activity.content.state
                let finalState = DeliveryActivityAttributes.ContentState(
                    caixasEntregues: call.getInt("caixasEntregues") ?? current.caixasEntregues,
                    totalCaixas: call.getInt("totalCaixas") ?? current.totalCaixas,
                    proximoCliente: call.getString("proximoCliente") ?? current.proximoCliente,
                    status: statusFinal
                )
                let content = ActivityContent(state: finalState, staleDate: nil)
                let policy: ActivityUIDismissalPolicy = .after(
                    Date().addingTimeInterval(dismissalSeconds)
                )
                await activity.end(content, dismissalPolicy: policy)
            }
            call.resolve([
                "stopped": true,
                "ended": true,
                "count": activities.count
            ])
        }
    }
}
