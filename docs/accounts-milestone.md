# Jalon comptes Enterprise

Ce document conserve l'état historique du premier jalon du 6 novembre. Le code a depuis ajouté les fonctions collaboratives et la reprise. Pour les protections actuelles d'inscription et de connexion, consulter [l'admission et le contexte client](auth-admission.md) ; les limites décrites ci-dessous expliquent le point de départ de la correction du 8 novembre.

Ce jalon ajoute une API de comptes opt-in, sans activer les fonctions collaboratives encore absentes. Le mode pédagogique legacy reste disponible par défaut ; en mode Enterprise ses mutations anonymes sont refusées. Les routes de comptes sont détaillées dans `auth-contract.md` et `openapi-v1.json`.

## Initialisation explicite

Créer un fichier v2 uniquement dans une destination absente (`V2Store(path).initialize()`) ou utiliser la migration vérifiée sur une copie du catalogue legacy. Aucun fichier de production n'est automatiquement remplacé ou recréé. Fournir `ENTERPRISE_ENABLED=true`, `ENTERPRISE_DATA_FILE` et un fichier privé d'au moins 32 octets via `AUTH_THROTTLE_KEY_FILE`. Ces fichiers ne doivent jamais être committés. Le contrôle `/health` lit le document Enterprise actif et ne modifie pas sa révision.

Le premier administrateur est créé localement par `python -m app.bootstrap --store /data/enterprise.json`. L'outil exige un terminal interactif, demande le secret deux fois sans l'afficher, refuse les mots de passe en argument ou sur un pipe, et n'ajoute aucun administrateur s'il en existe déjà un. Il n'existe aucun compte prédéfini. Ce mécanisme n'est pas une procédure de récupération administrative.

## Protections et limites

- Argon2id : temps 3, mémoire 64 Mio, parallélisme 2. Mots de passe 12–256 caractères et au plus 1024 octets UTF-8, sans troncature.
- Jeton opaque aléatoire ; seule une empreinte SHA-256 est persistée. Expiration absolue de huit heures et après trente minutes d'inactivité. Un retour arrière de l'horloge invalide la session concernée. Aucun bearer n'est accepté via l'URL ou un en-tête d'identité libre.
- État et rôle relus sous le verrou interprocessus du fichier v2. Changer un secret révoque toutes les sessions ; modifier rôle/statut révoque les sessions concernées. Deux rétrogradations concurrentes ne peuvent supprimer le dernier administrateur actif.
- Après cinq échecs de connexion par identifiant ou adresse source dans une fenêtre de quinze minutes, les essais reçoivent 429. Les clés des compteurs sont des HMAC, sans identifiant/adresse en clair. Derrière le frontend, l'adresse source API est celle du service web : cette limite agrège ses visiteurs. Une limitation complémentaire en amont reste à étudier avant une exposition publique. Les en-têtes proxy non authentifiés ne sont pas utilisés.
- Plafond de 10000 comptes, sessions et compteurs dans le validateur. L'épuisement de capacité provoque une indisponibilité contrôlée, sans corruption ; la stratégie d'exploitation et de limitation de l'inscription doit encore être renforcée avant exposition publique.
- Le JSON Schema décrit les formes ; le validateur Python garantit aussi unicité, références, ordre des horodatages et autres invariants transversaux.
- Ce jalon ne fournit pas encore l'audit métier, les collections, les prêts/réservations ni un transport HTTPS. HTTP de laboratoire ou loopback ne convient pas à des comptes réels sur un réseau non fiable.

## Validation réelle

59 tests API ont réussi sur Windows et sur Linux dans un conteneur Python 3.13. Les scénarios couvrent aussi les entrées invalides, la concurrence sur les administrateurs, le redémarrage de l'API, les erreurs de stockage et les schémas. La pile Compose avec frontend réel vérifie séparément le parcours inscription/connexion/profil, les rôles, le changement de secret, la révocation, la persistance, l'arrêt/reprise de l'API et le fonctionnement sous UID 10001. La preuve locale est conservée hors dépôt de code dans `work/evidence/09-accounts-compose/` du workspace de livraison ; ce test ne prouve ni Swarm ni fonctionnement entre VM. Les tests web simulant l'API restent distingués de cette intégration HTTP réelle.
