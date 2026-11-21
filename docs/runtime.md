# Image d'exécution

Le Dockerfile construit un runtime spécialisé pour CineVault en deux étapes : Python 3.13 Alpine fixé au digest pour les dépendances Python, puis Alpine 3.24 fixé au digest pour l'image finale. Les bibliothèques natives sont installées par APK avec sa base de paquets réelle ; zlib est fixé à `1.4-r0`. Le module standard `sqlite3` et son extension native sont omis avant copie dans l'étape finale, et `sqlite-libs` n'y est jamais installé. L’image prend en charge JSON et embarque le pilote PyMongo pur Python verrouillé pour MongoDB.

Les dépendances applicatives sont exactes dans requirements.txt, avec installation par roues binaires seulement. Les bases sont fixées au digest ; les autres fournisseurs de bibliothèques APK ne sont pas tous fixés individuellement. Deux reconstructions à des dates différentes peuvent donc résoudre des révisions système différentes. Conserver l'identifiant de chaque image et son inventaire de scan, puis revalider avant distribution ; ne pas revendiquer une reproductibilité bit à bit.

Le conteneur tourne en UID/GID 10001. Le fichier métier appartient au volume `/data`, les secrets sont injectés séparément. Les bibliothèques nécessaires à Argon2, au verrouillage, à HTTPS et à la compression restent disponibles. Les VM hôtes Ubuntu 24 restent distinctes du système de fichiers Alpine du conteneur.

## Sélection du stockage en mode Enterprise

`STORAGE_BACKEND=json` est la valeur par défaut. Fournir un `ENTERPRISE_DATA_FILE` v2
existant. L'application vérifie le fichier au démarrage.

Pour MongoDB, configurer `STORAGE_BACKEND=mongodb`, un `MONGODB_DATABASE` préprovisionné
et un `MONGODB_URI_FILE` monté en lecture seule et lisible par l'UID 10001. Le
fichier contient une URI standard `mongodb://` avec utilisateur, mot de passe,
`authSource`, `authMechanism=SCRAM-SHA-256` et `replicaSet` explicites.
`tls=true` avec validation des certificats est exigé, sauf pour une connexion de
qualification à un seul hôte de boucle locale avec `directConnection=true`.
Pour une autorité privée, monter sa CA et indiquer son chemin via `tlsCAFile`.
L'identité applicative n'effectue aucune provision ni modification de schéma.

Chaque worker construit et ferme son propre client. Démarrer Gunicorn sans
`--preload`. La sonde `/health` exécute une lecture de coordination dans une
transaction et renvoie 503 quand le stockage sélectionné est inaccessible.
Elle vérifie la disponibilité applicative instantanée.

`REGISTRATION_ADMISSION_FILE` est obligatoire en mode MongoDB. Les workers d'une
même instance API doivent partager son fichier et son verrou sur un volume
accessible en écriture. Lorsque les métadonnées sont activées, fournir aussi
`METADATA_CACHE_FILE`. Ces chemins sont distincts des fichiers métier et des
secrets. Ils contiennent respectivement les compteurs d'admission et le cache
fournisseur. La migration et les opérations d'administration se font séparément
du démarrage HTTP.

## Décision et validation locale

Le 7 novembre 2026, les scans complets ont refusé l'image Debian initiale, puis une image Python Alpine complète. Une image minimale a ensuite été évaluée dans un contexte isolé avant adoption : vrai HEALTHCHECK, UID 10001, scan Grype 0.121.2 sans avis ignoré, suite complète contre le code contenu dans l'image et intégration HTTP Compose des deux services. Les essais de harnais échoués ont été conservés : exécution d'un venv sur tmpfs noexec, puis oubli des schémas JSON dans le contexte de test ; aucune modification métier n'a servi à masquer ces erreurs.

Le dernier scan local a trouvé zéro High/Critical, cinq Medium et un Low, avec la base Grype datée du 7 novembre. Le seuil High/Critical est inchangé ; tous les avis restent dans le rapport. Ce résultat n'est pas une preuve d'absence de vulnérabilité et devra être renouvelé au build de livraison. Les vérifications locales ne constituent pas une exécution GitHub Actions ou un déploiement Swarm.

Les scripts et commandes de revalidation sont décrits dans [quality.md](quality.md). Les données, clés et archives de laboratoire restent hors du dépôt et du contexte de construction.


Une campagne isolée du 21 novembre 2026 a vérifié TLS 1.3 sur MongoDB réel.
Le certificat et l'identité SCRAM attendus donnent 200 à `/health`. Une CA,
un nom de serveur ou un mot de passe incorrects donnent 503. La révision
reste inchangée et les ressources temporaires sont retirées. La campagne
s'est déroulée dans un espace réseau privé à un seul hôte.
[Reçu TLS et disponibilité](evidence/mongodb-tls-native-20261121.json).

## Contrôle frais du 21 novembre 2026

Grype 0.121.2, base du 21 novembre à 06 h 20 UTC, a refusé le build local
de staging. Les cinq avis CVE-2026-107493, CVE-2026-107494, CVE-2026-121482,
CVE-2026-124709 et CVE-2026-84786 apparaissent sur chacun des deux paquets
Alpine `libcrypto3` et `libssl3` en version `3.5.9-r1`. Le démarrage, l'UID
et le nettoyage de la sonde ont réussi. Le contrôle High/Critical reste requis
avant de distribuer cette image ou de l'utiliser pour le staging.
L'index Alpine x86_64 consulté proposait les deux paquets en `3.5.9-r1`.
[Index Alpine](https://pkgs.alpinelinux.org/packages?arch=x86_64&branch=v3.24&name=openssl&repo=main).
[Versions OpenSSL publiées](https://www.openssl-library.org/source/).
