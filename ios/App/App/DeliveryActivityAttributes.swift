import Foundation
import ActivityKit

public struct DeliveryActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var caixasEntregues: Int
        public var totalCaixas: Int
        public var proximoCliente: String
        public var status: String

        public init(
            caixasEntregues: Int,
            totalCaixas: Int,
            proximoCliente: String,
            status: String
        ) {
            self.caixasEntregues = caixasEntregues
            self.totalCaixas = totalCaixas
            self.proximoCliente = proximoCliente
            self.status = status
        }
    }

    public var numeroCarga: String

    public init(numeroCarga: String) {
        self.numeroCarga = numeroCarga
    }
}
