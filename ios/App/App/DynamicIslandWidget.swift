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
                    HStack {
                        Image(systemName: "box.truck.badge.clock.fill")
                            .foregroundStyle(.green)
                        Text("Carga \(context.attributes.numeroCarga)")
                            .font(.caption)
                            .fontWeight(.bold)
                            .foregroundStyle(.white)
                    }
                }

                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.state.status)
                        .font(.caption2)
                        .fontWeight(.semibold)
                        .foregroundStyle(.green)
                }

                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Próxima parada: \(context.state.proximoCliente)")
                            .font(.subheadline)
                            .foregroundStyle(.white.opacity(0.9))
                            .lineLimit(1)
                    }
                }

                DynamicIslandExpandedRegion(.bottom) {
                    VStack(spacing: 4) {
                        DeliveryProgressBar(
                            delivered: context.state.caixasEntregues,
                            total: context.state.totalCaixas
                        )

                        Text("\(context.state.caixasEntregues) de \(context.state.totalCaixas) caixas entregues")
                            .font(.caption2)
                            .foregroundStyle(.white.opacity(0.85))
                    }
                }
            } compactLeading: {
                Image(systemName: "box.truck.fill")
                    .foregroundStyle(.green)
            } compactTrailing: {
                Text("\(context.state.caixasEntregues)/\(context.state.totalCaixas)")
                    .font(.caption2)
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
            HStack {
                Image(systemName: "truck.box.fill")
                    .font(.title3)
                    .foregroundStyle(.orange)

                VStack(alignment: .leading, spacing: 2) {
                    Text("Menino do Alho - Carga \(context.attributes.numeroCarga)")
                        .font(.headline)
                        .foregroundStyle(.white)

                    Text(context.state.status)
                        .font(.caption)
                        .foregroundStyle(.green)
                }

                Spacer()
            }

            Text("Próxima parada: \(context.state.proximoCliente)")
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.9))
                .lineLimit(2)

            DeliveryProgressBar(
                delivered: context.state.caixasEntregues,
                total: context.state.totalCaixas
            )

            Text("\(context.state.caixasEntregues) de \(context.state.totalCaixas) caixas entregues")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.75))
        }
        .padding(16)
        .activityBackgroundTint(Color(red: 0.12, green: 0.12, blue: 0.14))
        .activitySystemActionForegroundColor(.white)
    }
}

@available(iOS 16.1, *)
private struct DeliveryProgressBar: View {
    let delivered: Int
    let total: Int

    private var progress: Double {
        guard total > 0 else { return 0 }
        return min(Double(delivered) / Double(total), 1.0)
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(Color.white.opacity(0.2))

                Capsule()
                    .fill(Color.orange)
                    .frame(width: geometry.size.width * progress)
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
