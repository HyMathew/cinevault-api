# Extraction des commandes film — C2a

Objectif : exécuter création, remplacement et suppression de film via les mêmes
commandes métier JSON/Mongo, avec exemplaire initial atomique et versions ETag.
Les listes/recherche et commandes d’exemplaires avec reçu d’idempotence suivent
dans le lot C2b ; aucun profil HTTP Mongo incomplet ne sera activé entre les lots.

- [x] Écrire le contrat pur contre JsonPersistence et constater RED : droits,
  copie initiale, doublon Unicode, version absente/périmée, référence et suppression.
- [x] Extraire un exécuteur authentifié commun de la politique C1, sans dupliquer
  expiration, autorité, touch ou annulation d’identité invalide.
- [x] Déplacer validations film/exemplaire et ETag dans un module pur importable
  par les services et les anciens contrôleurs ; préserver leurs imports publics.
- [x] Raccorder les trois commandes HTTP au service JSON existant ; métadonnées
  externes et rendu HTTP restent hors du callback. Recontrôler autorité/version
  après préparation, préparer les UUID une seule fois avant tout retry.
- [x] Rejouer les contrats REST existants, le contrat pur, les garde-fous C1 puis
  la suite complète ; fournir un contrat stdlib réutilisable sur Mongo réel.
- [x] Vérifier le contrat sur le moteur isolé et relire le diff avant intégration.
  Revue source par l’orchestrateur ; revue indépendante globale encore à réaliser.

La sélection du backend, les requêtes publiques ciblées, idempotence des copies,
circulation et migration restent des critères ouverts du mandat global.

## Preuves du lot C2a

Contrat JSON observé RED (service absent), puis passé avec les garde-fous
existants. Suite complète : **307PASS/5SKIP/11sous-tests**,71.90s ; les5skips
locaux exigent un URI Mongo réel. Ruff108fichiers, format, docs et OpenAPI passent.
Le test de reprise injectée annule réellement le premier fichier transactionnel,
puis rejoue avec les mêmes UUID film/exemplaire : une seule paire est persistée.

Contrat natif exécuté le11novembre de12:45:52 à12:46:54UTC sur Mongo8.0.34,
avec compte applicatif CRUD et client de maintenance distincts. Permissions,
copie initiale, doublon insensible à la casse Unicode,428/412ETag,
409référence et204suppression : réussis. Le même essai rejoue l’authentification
C1 et la course admin avec un véritable conflit serveur112. Manifeste47sources :
`73d92abbe6a60356066f704e80f570a6aacde3c53d3e30a3c90f88a7fa2e0cba`.
Tous conteneurs et volumes de la sonde ont été supprimés ; aucun port publié,
accès aux données stables ou basculement de backend n’a été effectué.

Les commandes film utilisent des accès ciblés aux comptes/sessions/catalogue
et aux seules références nécessaires. La préparation HTTP autorise avant l’appel
externe, puis recontrôle rôle et version au commit ; les tests métadonnées
existants vérifient aussi ce retrait d’autorité pendant le chargement. La réponse
Flask et Location sont construits après commit. Les listes publiques et les
commandes d’exemplaires demeurent sur le magasin JSON transitoire jusqu’à C2b ;
le mode Mongo HTTP complet reste un critère de sortie distinct.
