#!/usr/bin/env bash
# Instala pods do app iOS e valida CapacitorLocalNotifications no Podfile.lock.
#
# Uso (Mac, na raiz do repositório):
#   npm install
#   npm run ios:setup-pods
#
# Se ios/App/Podfile não existir, gere-o antes com:
#   npx cap sync ios
# Projetos Capacitor 8 com CapApp-SPM podem não usar CocoaPods; nesse caso
# use npx cap sync ios e confira ios/App/CapApp-SPM/Package.swift.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IOS_APP="$ROOT/ios/App"
PODFILE="$IOS_APP/Podfile"
LOCKFILE="$IOS_APP/Podfile.lock"

echo "==> Sistema Menino do Alho — setup CocoaPods (LocalNotifications)"
echo "    Diretório iOS: $IOS_APP"

if [[ ! -d "$IOS_APP" ]]; then
  echo "Erro: pasta ios/App não encontrada. Execute este script na raiz do clone." >&2
  exit 1
fi

if [[ ! -f "$PODFILE" ]]; then
  echo "Erro: $PODFILE não existe." >&2
  echo "O Capacitor sync rápido (~0.03s) costuma indicar projeto SPM sem Podfile." >&2
  echo "Na raiz do projeto, tente:" >&2
  echo "  npm install" >&2
  echo "  npx cap sync ios" >&2
  echo "Se ainda não houver Podfile, confira ios/App/CapApp-SPM (Swift Package Manager)" >&2
  echo "ou recrie a plataforma iOS conforme a documentação do Capacitor 8." >&2
  exit 1
fi

if ! command -v pod >/dev/null 2>&1; then
  echo "Erro: comando 'pod' (CocoaPods) não encontrado no PATH." >&2
  echo "Instale no macOS, por exemplo:" >&2
  echo "  brew install cocoapods" >&2
  echo "ou:" >&2
  echo "  sudo gem install cocoapods" >&2
  echo "Depois rode novamente: npm run ios:setup-pods" >&2
  exit 1
fi

echo "==> pod install --repo-update (em ios/App)"
cd "$IOS_APP"
pod install --repo-update

if [[ ! -f "$LOCKFILE" ]]; then
  echo "Erro: pod install terminou mas $LOCKFILE não foi gerado." >&2
  exit 1
fi

if ! grep -q 'CapacitorLocalNotifications' "$LOCKFILE"; then
  echo "Erro: Podfile.lock não contém CapacitorLocalNotifications." >&2
  echo "Confirme no Podfile, dentro de capacitor_pods:" >&2
  echo "  pod 'CapacitorLocalNotifications', :path => '../../node_modules/@capacitor/local-notifications'" >&2
  echo "Em seguida: npx cap sync ios && npm run ios:setup-pods" >&2
  exit 1
fi

echo "Sucesso: Podfile.lock gerado e CapacitorLocalNotifications presente."
echo "Próximo passo: npx cap open ios (build no Xcode)."
