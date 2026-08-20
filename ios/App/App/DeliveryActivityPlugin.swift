import Foundation
import ActivityKit
import Capacitor

@objc(DeliveryActivityPlugin)
public class DeliveryActivityPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "DeliveryActivityPlugin"
    public let jsName = "DeliveryActivity"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "start", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "update", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop", returnType: CAPPluginReturnPromise)
    ]

    @objc func start(_ call: CAPPluginCall) {
        guard #available(iOS 16.1, *) else {
            call.reject("Live Activities exigem iOS 16.1 ou superior.")
            return
        }

        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            call.reject("Live Activities desativadas nas configurações do iPhone.")
            return
        }

        let numeroCarga = call.getString("numeroCarga") ?? "—"
        let placaVeiculo = call.getString("placaVeiculo") ?? ""
        let proximoCliente = call.getString("proximoCliente") ?? "—"
        let totalCaixas = call.getInt("totalCaixas") ?? 0
        let caixasEntregues = call.getInt("caixasEntregues") ?? 0
        let status = call.getString("status") ?? "Em rota"

        let attributes = DeliveryActivityAttributes(
            numeroCarga: numeroCarga,
            placaVeiculo: placaVeiculo
        )
        let state = DeliveryActivityAttributes.ContentState.make(
            status: status,
            proximoCliente: proximoCliente,
            caixasEntregues: caixasEntregues,
            totalCaixas: totalCaixas
        )

        do {
            // Encerra atividades anteriores da mesma carga para evitar duplicatas.
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

    @objc func update(_ call: CAPPluginCall) {
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
        let caixasEntregues = call.getInt("caixasEntregues") ?? current.caixasEntregues
        let totalCaixas = call.getInt("totalCaixas") ?? current.totalCaixas
        let proximoCliente = call.getString("proximoCliente") ?? current.proximoCliente
        let status = call.getString("status") ?? current.status

        let state = DeliveryActivityAttributes.ContentState.make(
            status: status,
            proximoCliente: proximoCliente,
            caixasEntregues: caixasEntregues,
            totalCaixas: totalCaixas
        )

        Task {
            await activity.update(ActivityContent(state: state, staleDate: nil))
            call.resolve([
                "activityId": activity.id,
                "updated": true,
                "progresso": state.progresso
            ])
        }
    }

    @objc func stop(_ call: CAPPluginCall) {
        guard #available(iOS 16.1, *) else {
            call.reject("Live Activities exigem iOS 16.1 ou superior.")
            return
        }

        let activities = Activity<DeliveryActivityAttributes>.activities
        guard !activities.isEmpty else {
            call.resolve(["stopped": true, "count": 0])
            return
        }

        let statusFinal = call.getString("status") ?? "Finalizado"
        let dismissalSeconds = call.getDouble("dismissalSeconds") ?? 5

        Task {
            for activity in activities {
                let current = activity.content.state
                let finalState = DeliveryActivityAttributes.ContentState.make(
                    status: statusFinal,
                    proximoCliente: current.proximoCliente.isEmpty ? "Concluído" : current.proximoCliente,
                    caixasEntregues: call.getInt("caixasEntregues") ?? current.caixasEntregues,
                    totalCaixas: call.getInt("totalCaixas") ?? current.totalCaixas
                )
                let content = ActivityContent(state: finalState, staleDate: nil)
                let policy: ActivityUIDismissalPolicy = .after(
                    Date().addingTimeInterval(dismissalSeconds)
                )
                await activity.end(content, dismissalPolicy: policy)
            }
            call.resolve([
                "stopped": true,
                "count": activities.count
            ])
        }
    }
}
