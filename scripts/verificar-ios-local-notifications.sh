#!/usr/bin/env bash
# Checagem do alvo nativo CapacitorLocalNotifications.
#
# Este app iOS usa Swift Package Manager (ios/App/CapApp-SPM).
# O Capacitor CLI só executa `pod install` quando existe ios/App/Podfile.
# Neste repositório o Podfile não existe de propósito: misturar CocoaPods
# com CapApp-SPM duplica o plugin e quebra o link.
#
# No Mac:
#   npm install
#   npm run ios:check-local-notifications
#   npx cap sync ios
#
# Se um dia o projeto migrar para CocoaPods, o script passa a rodar
# `pod install` dentro de ios/App e exige o alvo CapacitorLocalNotifications.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

POD_LINE="pod 'CapacitorLocalNotifications', :path => '../../node_modules/@capacitor/local-notifications'"

echo "==> Dependência npm"
node -e "
const pkg = require('./package.json');
const nome = '@capacitor/local-notifications';
const dep = pkg.dependencies && pkg.dependencies[nome];
const dev = pkg.devDependencies && pkg.devDependencies[nome];
if (!dep) {
  console.error(nome + ' precisa estar em dependencies, não só em devDependencies.');
  process.exit(1);
}
if (dev) {
  console.error(nome + ' está em devDependencies. Mova para dependencies.');
  process.exit(1);
}
console.log(nome + '@' + dep + ' em dependencies.');
"

echo "==> Pacote iOS do plugin"
PODSPEC="node_modules/@capacitor/local-notifications/CapacitorLocalNotifications.podspec"
PKG_SWIFT_PLUGIN="node_modules/@capacitor/local-notifications/Package.swift"
if [[ ! -f "$PODSPEC" || ! -f "$PKG_SWIFT_PLUGIN" ]]; then
  echo "Plugin ausente em node_modules. Rode npm install na raiz do projeto." >&2
  exit 1
fi
echo "podspec e Package.swift do plugin encontrados."

echo "==> Registro no app (SPM + capacitor.config)"
SPM="ios/App/CapApp-SPM/Package.swift"
IOS_CONFIG="ios/App/App/capacitor.config.json"
if [[ ! -f "$SPM" ]]; then
  echo "Ausente: $SPM" >&2
  exit 1
fi
if ! grep -q 'CapacitorLocalNotifications' "$SPM"; then
  echo "Package.swift não referencia o produto CapacitorLocalNotifications." >&2
  echo "No Mac: npx cap sync ios" >&2
  exit 1
fi
if [[ ! -f "$IOS_CONFIG" ]] || ! grep -q 'LocalNotificationsPlugin' "$IOS_CONFIG"; then
  echo "ios/App/App/capacitor.config.json não lista LocalNotificationsPlugin." >&2
  echo "No Mac: npx cap sync ios" >&2
  exit 1
fi
echo "SPM e packageClassList incluem LocalNotifications."

PODFILE="ios/App/Podfile"
if [[ -f "$PODFILE" ]]; then
  echo "==> CocoaPods (ios/App/Podfile)"
  if ! grep -q "CapacitorLocalNotifications" "$PODFILE"; then
    echo "O Podfile não inclui o alvo CapacitorLocalNotifications." >&2
    echo "Adicione dentro de capacitor_pods, em ios/App/Podfile:" >&2
    echo "  $POD_LINE" >&2
    exit 1
  fi
  if ! command -v pod >/dev/null 2>&1; then
    echo "CocoaPods não está instalado. No Mac:" >&2
    echo "  sudo gem install cocoapods" >&2
    echo "  cd ios/App && pod install" >&2
    exit 1
  fi
  echo "Rodando pod install em ios/App"
  (cd ios/App && pod install)
  if [[ ! -f ios/App/Podfile.lock ]] || ! grep -q "CapacitorLocalNotifications" ios/App/Podfile.lock; then
    echo "pod install terminou sem registrar CapacitorLocalNotifications no Podfile.lock." >&2
    exit 1
  fi
  echo "Podfile.lock contém CapacitorLocalNotifications."
else
  echo "==> CocoaPods"
  echo "Não há ios/App/Podfile. Este projeto usa SPM; npx cap sync ios não chama pod install."
  echo "Não crie um Podfile ao lado de CapApp-SPM."
  echo
  echo "No Mac, sincronize o alvo já declarado em CapApp-SPM/Package.swift:"
  echo "  npm install"
  echo "  npx cap sync ios"
  echo "  npx cap open ios"
  echo
  echo "Se migrar o app para CocoaPods, o Podfile precisa desta linha antes de pod install:"
  echo "  $POD_LINE"
  echo "Depois: cd ios/App && pod install"
fi

echo "Checagem de LocalNotifications concluída."
