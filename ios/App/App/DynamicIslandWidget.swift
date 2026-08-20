import ActivityKit
import SwiftUI
import WidgetKit

@available(iOS 16.1, *)
struct DeliveryActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: DeliveryActivityAttributes.self) { context in
            DeliveryLockScreenView(context: context)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Carga #\(context.attributes.numeroCarga)")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundStyle(.white)
                            .lineLimit(1)
                        if !context.attributes.placaVeiculo.isEmpty {
                            Text(context.attributes.placaVeiculo)
                                .font(.caption2)
                                .foregroundStyle(.white.opacity(0.75))
                                .lineLimit(1)
                        }
                    }
                }

                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.state.status)
                        .font(.caption2)
                        .fontWeight(.semibold)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Capsule().fill(Color.green.opacity(0.85)))
                }

                DynamicIslandExpandedRegion(.center) {
                    Text("Próxima parada: \(context.state.proximoCliente)")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.9))
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                DynamicIslandExpandedRegion(.bottom) {
                    VStack(spacing: 6) {
                        DeliveryProgressBar(progress: context.state.progresso)
                        Text("\(context.state.caixasEntregues) de \(context.state.totalCaixas) caixas entregues")
                            .font(.caption2)
                            .foregroundStyle(.white.opacity(0.85))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            } compactLeading: {
                Image(systemName: "box.truck.badge.clock.fill")
                    .foregroundStyle(.green)
            } compactTrailing: {
                Text("\(context.state.caixasEntregues)/\(context.state.totalCaixas) cx")
                    .font(.caption2)
                    .fontWeight(.semibold)
                    .foregroundStyle(.white)
            } minimal: {
                Image(systemName: "box.truck.fill")
                    .foregroundStyle(.green)
            }
        }
    }
}

@available(iOS 16.1, *)
private struct DeliveryLockScreenView: View {
    let context: ActivityViewContext<DeliveryActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                Image(systemName: "box.truck.badge.clock.fill")
                    .font(.title3)
                    .foregroundStyle(.green)

                VStack(alignment: .leading, spacing: 2) {
                    Text("Menino do Alho")
                        .font(.caption)
                        .fontWeight(.semibold)
                        .foregroundStyle(.green)

                    Text("Carga #\(context.attributes.numeroCarga)")
                        .font(.headline)
                        .foregroundStyle(.white)

                    if !context.attributes.placaVeiculo.isEmpty {
                        Text("Placa \(context.attributes.placaVeiculo)")
                            .font(.caption2)
                            .foregroundStyle(.white.opacity(0.7))
                    }
                }

                Spacer()

                Text(context.state.status)
                    .font(.caption2)
                    .fontWeight(.semibold)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Capsule().fill(Color.green.opacity(0.85)))
            }

            Text("Próxima parada: \(context.state.proximoCliente)")
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.9))
                .lineLimit(2)

            DeliveryProgressBar(progress: context.state.progresso)

            Text("\(context.state.caixasEntregues) de \(context.state.totalCaixas) caixas entregues")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.75))
        }
        .padding(16)
        .activityBackgroundTint(Color(red: 0.10, green: 0.12, blue: 0.14))
        .activitySystemActionForegroundColor(.white)
    }
}

@available(iOS 16.1, *)
private struct DeliveryProgressBar: View {
    let progress: Double

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(Color.white.opacity(0.2))

                Capsule()
                    .fill(Color.green)
                    .frame(width: geometry.size.width * max(0, min(progress, 1)))
            }
        }
        .frame(height: 6)
    }
}

@main
@available(iOS 16.1, *)
struct DeliveryWidgetBundle: WidgetBundle {
    var body: some Widget {
        DeliveryActivityWidget()
    }
}
