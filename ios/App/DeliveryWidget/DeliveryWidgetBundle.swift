import SwiftUI
import WidgetKit

@main
@available(iOS 16.1, *)
struct DeliveryWidgetBundle: WidgetBundle {
    var body: some Widget {
        DeliveryActivityWidget()
        QuickActionsWidget()
    }
}
