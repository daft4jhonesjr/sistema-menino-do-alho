import SwiftUI
import WidgetKit

struct QuickActionsEntry: TimelineEntry {
    let date: Date
}

struct QuickActionsProvider: TimelineProvider {
    func placeholder(in context: Context) -> QuickActionsEntry {
        QuickActionsEntry(date: Date())
    }

    func getSnapshot(in context: Context, completion: @escaping (QuickActionsEntry) -> Void) {
        completion(QuickActionsEntry(date: Date()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<QuickActionsEntry>) -> Void) {
        let entry = QuickActionsEntry(date: Date())
        completion(Timeline(entries: [entry], policy: .never))
    }
}

struct QuickActionsWidget: Widget {
    let kind = "QuickActionsWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: QuickActionsProvider()) { entry in
            QuickActionsWidgetView(entry: entry)
        }
        .configurationDisplayName("Atalhos")
        .description("Nova venda, lançamento de caixa e logística.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

private struct QuickActionsWidgetView: View {
    @Environment(\.widgetFamily) private var family

    let entry: QuickActionsEntry

    private let fundo = Color(red: 0.09, green: 0.11, blue: 0.14)
    private let esmeralda = Color(red: 0.063, green: 0.725, blue: 0.506)

    var body: some View {
        conteudo
            .widgetBackground(fundo)
    }

    @ViewBuilder
    private var conteudo: some View {
        if family == .systemMedium {
            layoutMedio
        } else {
            layoutPequeno
        }
    }

    private var layoutPequeno: some View {
        VStack(alignment: .leading, spacing: 4) {
            cabecalho
            VStack(spacing: 4) {
                atalho(
                    titulo: "Nova Venda",
                    icone: "cart.badge.plus",
                    destino: "meninodoalho://vendas/novo",
                    horizontal: true
                )
                atalho(
                    titulo: "Novo Caixa",
                    icone: "dollarsign.circle",
                    destino: "meninodoalho://caixa?acao=novo",
                    horizontal: true
                )
                atalho(
                    titulo: "Logística / Rota",
                    icone: "shippingbox.fill",
                    destino: "meninodoalho://logistica",
                    horizontal: true
                )
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .id(entry.date)
    }

    private var layoutMedio: some View {
        VStack(alignment: .leading, spacing: 10) {
            cabecalho
            HStack(spacing: 8) {
                atalho(
                    titulo: "Nova Venda",
                    icone: "cart.badge.plus",
                    destino: "meninodoalho://vendas/novo",
                    horizontal: false
                )
                atalho(
                    titulo: "Novo Caixa",
                    icone: "dollarsign.circle",
                    destino: "meninodoalho://caixa?acao=novo",
                    horizontal: false
                )
                atalho(
                    titulo: "Logística / Rota",
                    icone: "shippingbox.fill",
                    destino: "meninodoalho://logistica",
                    horizontal: false
                )
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var cabecalho: some View {
        HStack(spacing: 6) {
            Image(systemName: "leaf.fill")
                .font(.caption)
                .foregroundStyle(esmeralda)
                .accessibilityHidden(true)
            Text("Menino do Alho")
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(.white.opacity(0.86))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder
    private func atalho(titulo: String, icone: String, destino: String, horizontal: Bool) -> some View {
        if let url = URL(string: destino) {
            Link(destination: url) {
                Group {
                    if horizontal {
                        HStack(spacing: 8) {
                            iconeView(icone)
                            tituloView(titulo, alinhamento: .leading)
                            Spacer(minLength: 0)
                        }
                    } else {
                        VStack(spacing: 8) {
                            iconeView(icone)
                            tituloView(titulo, alinhamento: .center)
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: horizontal ? .leading : .center)
                .padding(.horizontal, horizontal ? 8 : 6)
                .padding(.vertical, 6)
                .background {
                    ContainerRelativeShape()
                        .fill(Color.white.opacity(0.08))
                }
                .overlay {
                    ContainerRelativeShape()
                        .stroke(esmeralda.opacity(0.45), lineWidth: 1)
                }
            }
            .accessibilityLabel(titulo)
        }
    }

    private func iconeView(_ nome: String) -> some View {
        Image(systemName: nome)
            .font(.title3)
            .foregroundStyle(esmeralda)
            .accessibilityHidden(true)
    }

    private func tituloView(_ titulo: String, alinhamento: TextAlignment) -> some View {
        Text(titulo)
            .font(.caption)
            .fontWeight(.semibold)
            .foregroundStyle(.white)
            .multilineTextAlignment(alinhamento)
            .lineLimit(2)
            .minimumScaleFactor(0.7)
    }
}

private extension View {
    @ViewBuilder
    func widgetBackground(_ color: Color) -> some View {
        if #available(iOS 17.0, *) {
            containerBackground(color, for: .widget)
        } else {
            background(ContainerRelativeShape().fill(color))
        }
    }
}
