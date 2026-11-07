# Image d'exécution

Le Dockerfile construit un runtime spécialisé pour CineVault en deux étapes : Python 3.13 Alpine fixé au digest pour les dépendances Python, puis Alpine 3.24 fixé au digest pour l'image finale. Les bibliothèques natives sont installées par APK avec sa base de paquets réelle ; zlib est fixé à `1.4-r0`. Le module standard `sqlite3` et son extension native sont omis avant copie dans l'étape finale, et `sqlite-libs` n'y est jamais installé. CineVault conserve son stockage JSON ; cette image n'est pas un environnement Python général avec tous les modules optionnels.

Les dépendances applicatives sont exactes dans requirements.txt, avec installation par roues binaires seulement. Les bases sont fixées au digest ; les autres fournisseurs de bibliothèques APK ne sont pas tous fixés individuellement. Deux reconstructions à des dates différentes peuvent donc résoudre des révisions système différentes. Conserver l'identifiant de chaque image et son inventaire de scan, puis revalider avant distribution ; ne pas revendiquer une reproductibilité bit à bit.

Le conteneur tourne en UID/GID 10001. Le fichier métier appartient au volume `/data`, les secrets sont injectés séparément. Les bibliothèques nécessaires à Argon2, au verrouillage, à HTTPS et à la compression restent disponibles. Les VM hôtes Ubuntu 24 restent distinctes du système de fichiers Alpine du conteneur.

## Décision et validation locale

Le 7 novembre 2026, les scans complets ont refusé l'image Debian initiale, puis une image Python Alpine complète. Une image minimale a ensuite été évaluée dans un contexte isolé avant adoption : vrai HEALTHCHECK, UID 10001, scan Grype 0.121.2 sans avis ignoré, suite complète contre le code contenu dans l'image et intégration HTTP Compose des deux services. Les essais de harnais échoués ont été conservés : exécution d'un venv sur tmpfs noexec, puis oubli des schémas JSON dans le contexte de test ; aucune modification métier n'a servi à masquer ces erreurs.

Le dernier scan local a trouvé zéro High/Critical, cinq Medium et un Low, avec la base Grype datée du 7 novembre. Le seuil High/Critical est inchangé ; tous les avis restent dans le rapport. Ce résultat n'est pas une preuve d'absence de vulnérabilité et devra être renouvelé au build de livraison. Les vérifications locales ne constituent pas une exécution GitHub Actions ou un déploiement Swarm.

Les scripts et commandes de revalidation sont décrits dans [quality.md](quality.md). Les données, clés et archives de laboratoire restent hors du dépôt et du contexte de construction.
