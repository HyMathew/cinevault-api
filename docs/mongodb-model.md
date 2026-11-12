# Contrat du modèle MongoDB

État : fondation en développement, aucun basculement de données. Le schéma JSON
académique reste la référence des formes publiques ; les références et transitions
restent des règles métier, qui ne peuvent pas être remplacées par `$jsonSchema`.

Le [lot de lectures publiques C2c](public-catalogue-service.md) ajoute les clés
techniques de recherche/tri des films et trois index non uniques (25 au total).
Le nouveau validateur impose ces clés ; une ancienne base Mongo nécessite une
migration explicite. `provision` refuse sa divergence sans collMod ou réparation.
Les preuves datées ci-dessous portant sur22index restent celles de l’ancien lot.

Le [lot collections/audit C3](personal-service.md) ajoute un ordinal BSON Int64
explicite aux événements et remplace leur index timestamp/UUID par un index
unique d’ordre d’append. Le total reste25index ; une ancienne base sans ordinal
nécessite également une migration explicite. Les preuves C2c ne démontrent pas
ce nouveau validateur ni sa rétention transactionnelle.

## Représentation

Un document MongoDB correspond à une entité. Son `_id` est l'UUID canonique public,
stocké comme chaîne ; les compteurs d'authentification utilisent leur clé HMAC.
Les champs `id` ou `key` sont reconstruits à la sortie. Aucun ObjectId implicite.
Les collections privées sont stockées dans `collection_entries`, avec une clé
technique `list_name_key` calculée par `str.casefold()`, conformément à la règle
existante ; cette clé ne sort pas dans le contrat REST.

Tous les entiers persistés sont des BSON Int64, y compris les années, révisions,
séquences et horodatages. Les dates métier restent des **secondes UTC depuis
l'époque Unix** ; elles ne deviennent ni des dates locales ni des millisecondes.
Cette décision préserve notamment les valeurs de `external_source.retrieved_at`
déjà admises jusqu'à 2^53, qui dépassent les dates Python. Aucun TTL asynchrone
n'est utilisé pour décider de l'expiration d'une session ou d'un reçu.

Le codec convertit explicitement Int64 vers `int` avant les validateurs métier,
qui distinguent actuellement `type(x) is int` et `bool`. Un entier hors de
[-2^63, 2^63-1] est rejeté avant écriture, sans troncature ; une migration doit
signaler ce cas dans sa simulation. Le fichier JSON n'acquiert pas cette limite
BSON. Flottants, dates Python, clés d'opérateur et chaînes Unicode non encodables
ne constituent pas un format de persistance accepté.

## Validations et index

Les schémas documentaires dérivent des formes d'entités du schéma académique
versionné. Le compilateur n'accepte que les mots-clés explicitement pris en
charge ; un nouveau mot-clé inconnu fait échouer sa compilation. Les conditions
JSON Schema sont traduites en implications logiques compatibles MongoDB.
Les limites de taille des tableaux globaux du fichier ne deviennent pas des
limites de nombre de documents MongoDB.

Les index uniques protègent les identités, usernames, empreintes de session,
entrées privées normalisées, exemplaires en prêt actif, paires de prêts ouverts,
séquences de réservation, attentes et reçus. Ils ne prouvent pas les références
entre collections, le dernier administrateur, les quotas ou FIFO. Une unité de
travail transactionnelle devra encore assurer ces règles et coordonner les
commandes concurrentes. L'installation des schémas/index relève de la maintenance,
pas de l'identité applicative.

## Lot exécutable

- [x] Écrire puis observer les tests de codec BSON et de compilation des dix formes.
- [x] Implémenter `app/persistence/mongo_model.py` sans modifier les routes.
- [x] Éprouver le vrai encodage BSON et les contre-exemples de perte de précision,
  UUID, clés réservées, noms normalisés et propriétés conditionnelles.
- [x] Faire accepter les schémas/index par le moteur réel et vérifier ses refus.
- [ ] Intégrer ensuite repositories/UoW et sélection exclusive du moteur, avec les
  mêmes services et tests de contrat sur les deux moteurs.

Les tests de codec seuls ne valident ni transaction métier, migration, sauvegarde,
restauration, confinement réseau, ni compatibilité de l'image applicative Linux.

Essai natif exécuté le 10 novembre 2026, 14:08:44–14:09:21 UTC, sur MongoDB
8.0.34 isolé sans réseau ni port publié : dix validateurs, 22 index déclarés et
un jeu d'entités synthétiques liées acceptés. Le serveur a refusé les types et
champs invalides (121), puis les doublons username, copie active, prêt ouvert,
attente, prêt fulfilled, entrée privée et reçu (11000). Un prêt retourné pour la
même copie, une clé casefold de 240 caractères et 2^53 secondes ont été acceptés.
Conteneur et deux volumes neufs supprimés, API stable restée healthy.

Deux erreurs du harnais ont précédé cet essai réussi : la lecture EJSON doit
désactiver le mode relaxed pour garder Int64 ; la fonction de refus utilise une
fermeture lexicale dans le script mongosh. Les premiers essais ne constituent pas
des succès et leurs reçus restent conservés. Aucun validateur n'a été affaibli.

Un second essai, 14:09:57–14:11:05 UTC, a ajouté un vrai client PyMongo 4.19.0
sous CPython 3.13.18/Alpine musl, UID 10001 et extensions C désactivées. Sa wheel
locale a été produite deux fois à l'identique depuis le sdist contrôlé, puis
installée hors ligne avec hashes. Le client partageait seulement la boucle locale
du conteneur Mongo sans réseau externe : connexion authentifiée, lecture Int64,
refus des privilèges utilisateurs/index, validation native et transactions
film+copie commit/abort réussis. Les quatre ressources temporaires ont été
supprimées. Ce contrôle de compatibilité ne constitue pas le backend REST,
l'image applicative finale ou un benchmark.
