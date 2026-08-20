import Foundation
import ActivityKit

public struct DeliveryActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var status: String
        public var proximoCliente: String
        public var caixasEntregues: Int
        public var totalCaixas: Int
        public var progresso: Double

        public init(
            status: String,
            proximoCliente: String,
            caixasEntregues: Int,
            totalCaixas: Int,
            progresso: Double
        ) {
            self.status = status
            self.proximoCliente = proximoCliente
            self.caixasEntregues = caixasEntregues
            self.totalCaixas = totalCaixas
            self.progresso = progresso
        }

        public static func make(
            status: String,
            proximoCliente: String,
            caixasEntregues: Int,
            totalCaixas: Int
        ) -> ContentState {
            let progresso: Double
            if totalCaixas > 0 {
                progresso = min(Double(caixasEntregues) / Double(totalCaixas), 1.0)
            } else {
                progresso = 0
            }
            return ContentState(
                status: status,
                proximoCliente: proximoCliente,
                caixasEntregues: caixasEntregues,
                totalCaixas: totalCaixas,
                progresso: progresso
            )
        }
    }

    public var numeroCarga: String
    public var placaVeiculo: String

    public init(numeroCarga: String, placaVeiculo: String = "") {
        self.numeroCarga = numeroCarga
        self.placaVeiculo = placaVeiculo
    }
}
