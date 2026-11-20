# Migration opérateur JSON v2 et MongoDB

La commande `python -m app.migrate_backends` prépare un basculement
volontaire entre les deux représentations. Elle s'appuie sur le
[codec d'instantanés](mongodb-migration-codec.md) et le
[runner transactionnel](persistence-uow.md). Installer les dépendances
verrouillées de l'API et `requirements-mongodb.txt`. La simulation JSON utilise
la bibliothèque BSON localement et fonctionne sans serveur ni URI.

## Préparer puis importer

Isoler la base candidate du trafic de l'application. Arrêter et vérifier tous
les écrivains JSON et Mongo, y compris les tâches de maintenance et les DDL,
avant l'application. Maintenir cet arrêt jusqu'à la décision de basculement.
L'option `--isolated-and-writers-stopped` enregistre cette attestation
opérateur. C'est l'orchestration du déploiement qui doit effectuer l'arrêt.

Préparer la base neuve avec `app.persistence.mongo_maintenance.provision`
depuis un compte de maintenance distinct. Le pilote de migration vérifie le
modèle complet préprovisionné et ses index par des lectures. L'identité
d'import a besoin de lire les collections et leurs métadonnées, d'insérer dans
les collections métier et le registre, et de mettre à jour le coordinateur.
L'identité d'inspection et d'export peut être limitée aux lectures. Injecter
l'URI dans la variable `CINEVAULT_MIGRATION_MONGODB_URI` pour le seul processus
de migration, via le mécanisme de secrets de l'opérateur. Appliquer les
[mêmes contrôles d’URI que le runtime](runtime.md), avec une identité de migration
dédiée. Éviter le copier
coller d'une URI dans une commande ou un historique de shell. Le rapport
contient des empreintes et des cardinalités. Le conserver avec les preuves
privées du basculement.

```sh
python -m app.migrate_backends inspect-json --source original.json
```

Cette simulation valide le modèle et calcule l'empreinte des octets exacts,
sans créer de fichier ni de verrou. Retenir `source_sha256` dans le manifeste
privé. Créer une copie séparée avec `V2Store(source).snapshot(backup)` et
vérifier qu'elle a la même empreinte. Protéger l'original et sa sauvegarde avec
les permissions du stockage confidentiel. Vérifier aussi les consommateurs
de l'ordre historique des films et les éventuelles clés naturelles ambiguës.

```sh
python -m app.migrate_backends apply-forward \
  --source original.json --backup backup.json \
  --expected-source-sha256 "$SOURCE_SHA256" --database cinevault_candidate \
  --isolated-and-writers-stopped
```

L'import prend le même verrou fichier que `V2Store` et relit l'original et la
copie avant d'accéder à la cible. L'inventaire Mongo contrôle les douze
collections, leurs validateurs stricts et les spécifications d'index exactes
du modèle. Le serveur impose que les commandes d'inventaire des collections
et index s'exécutent hors transaction. Les DDL doivent donc rester arrêtés
pendant toutes les étapes. [Restrictions des transactions MongoDB](https://www.mongodb.com/docs/manual/core/transactions-operations/)

La première opération transactionnelle modifie réellement le coordinateur en
inversant son bit de poids faible. Le reste de la transaction ne démarre que
dans une base vide provisionnée à révision zéro. Les lots, le registre,
le rétablissement exact de la révision source et la vérification du contenu
restent dans la même transaction. Un candidat déjà identique produit
`already_applied` et annule la modification de verrou. Un candidat partiel
ou différent est refusé et ses données sont conservées. [Verrou d'écriture en transaction](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/)

Le code de sortie 3 signale un commit dont le résultat reste indéterminé.
Laisser les écrivains arrêtés et inspecter la cible en lecture seule.

```sh
python -m app.migrate_backends reconcile-forward \
  --source original.json --expected-source-sha256 "$SOURCE_SHA256" \
  --database cinevault_candidate
```

`matched` confirme l'état attendu dans un instantané lu. Un résultat
différent, y compris une base toujours vide, ne prouve pas que le commit
précédent est absent. Conserver les preuves et établir l'état de la
transaction et de la cible avant toute autre écriture.

## Exporter l'état Mongo courant vers un fichier de retour

Arrêter les écrivains Mongo et les DDL et garder l'original arrêté. L'inspection
réunit les douze collections en un instantané cohérent. Conserver son
`snapshot_sha256` privé, qui inclut les valeurs d'ordinal BSON, et le
`candidate_sha256` attendu pour le fichier.

```sh
python -m app.migrate_backends inspect-mongo --database cinevault_candidate

python -m app.migrate_backends apply-reverse \
  --database cinevault_candidate --original original.json \
  --expected-original-sha256 "$SOURCE_SHA256" \
  --expected-snapshot-sha256 "$MONGO_SNAPSHOT_SHA256" \
  --destination retour.json --isolated-and-writers-stopped
```

Le pilote relit l'instantané avant d'exporter. Une modification de son
empreinte ou de celle de l'original empêche la création. La publication
atomique crée un fichier distinct sans remplacer une destination existante.
Une destination contenant déjà les mêmes octets produit `already_exported`.
La protection des empreintes repose sur une source de confiance distincte.
L'arrêt des écrivains protège l'intervalle entre le snapshot Mongo et la
création du fichier, qui concernent deux moteurs séparés.

## Validation P5 avant exploitation

L'outil est destiné à un instantané maîtrisé. Il matérialise les collections
en mémoire, limite la transaction à 45 secondes et annule les échecs.
Les plafonds JSON actuels peuvent bloquer un retour. Dimensionner l'essai sur
une copie synthétique représentative et vérifier les limites avant le
basculement. Une révision au maximum Int64 reste transportable et ne permet
plus de nouvelle écriture Mongo selon le contrat de l'unité de travail.

Dans la campagne d'intégration, mesurer l'inventaire avant et après aller,
jouer l'aller une deuxième fois, puis vérifier de nouvelles écritures via
l'API sélectionnée sur Mongo. Tester aussi les erreurs de permissions, de
validation et de confirmation. Arrêter à nouveau les écritures, exporter
l'état actuel, vérifier la révision, les reçus, les références et l'ordre
d'audit, puis redémarrer l'API JSON sur ce fichier neuf et rejouer les
parcours métier. Le rapport doit distinguer les essais par doubles des
exécutions natives. Les tests locaux du pilote couvrent la commande, les
fichiers réels et le protocole transactionnel avec un double. La campagne
native et les parcours HTTP après basculement restent à exécuter.
