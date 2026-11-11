# Gate CI sur un vrai moteur MongoDB

Le job `mongodb` exécute [check_mongodb.py](../scripts/check_mongodb.py) sur un hôte
Linux non privilégié disposant de Docker et des dépendances de développement
verrouillées. Le script crée son propre serveur jetable ; il n’accepte aucune URI
d’un serveur existant et ne touche pas au laboratoire ou au magasin stable.
Les jobs image et publication dépendent aussi de ce gate.

## Déploiement jetable

Image MongoDB 8.0.34, Linux/amd64, épinglée au digest
`sha256:1d7a2d487c6465ea022474b90b65d6c91bb600b421b65880d8c6e141d6122200`.
Un replica set `cinevault-ci` à un membre fournit les transactions utilisées par
l’adaptateur ; cela ne constitue pas de la haute disponibilité.
L’authentification SCRAM et un keyfile aléatoire sont actifs. Le bootstrap utilise
uniquement l’exception localhost depuis `mongosh` dans le conteneur.

L’hôte ne publie qu’un port éphémère sur `127.0.0.1`, jamais `0.0.0.0`. La connexion
du driver est directe au membre publié ; la découverte des autres membres n’est
pas requise pour cette topologie de test. Le compte hôte courant, sans capacités
Linux, exécute `mongod` dans un système de fichiers racine en lecture seule ; ses
seules données sont un répertoire temporaire neuf, avec 768 Mio/0,75 CPU et cache
WiredTiger de 0,256 Gio.

Le keyfile est limité à son propriétaire (0400), sous un répertoire 0700 hors Git.
Les mots de passe et URI restent en mémoire ou dans l’environnement du processus
de test ; aucune valeur secrète n’est passée comme argument de commande. Les
directives de masquage GitHub précèdent les tests et les rapports sont assainis
avant conservation. Les exceptions publiques n’incluent ni URI ni réponse brute
du driver. Aucun journal serveur brut n’est téléversé.
En cas d’échec, le reçu conserve la phase, le dernier refus enfant assaini
(4096 caractères par flux), l’état du serveur possédé (dont exit/OOM) et au plus
80 lignes de journaux assainis, bornées à 12288 caractères. Les arguments et
l’entrée standard des commandes ne sont jamais conservés. L’assainissement
précède la troncature afin de ne pas exposer un fragment de secret long.

## Contrôles réellement demandés au moteur

- Un compte de maintenance crée les bases synthétiques et exécute les cinq cas
  `RealMongoTests` : contrat UoW, concurrence dernier administrateur, références et
  erreur native masquée, refus de validateur divergent, absence de coordination.
- Une seconde identité ne reçoit que find/insert/update/remove sur les douze
  collections explicitement utilisées. Le même contrat CRUD/transactions est
  exécuté avec cette identité ; le provisionnement reste à la maintenance.
- Création d’index/utilisateur, accès à une autre base et lecture anonyme doivent
  être refusés avec le code MongoDB 13.
- Le JUnit doit contenir au moins cinq tests, sans skip, erreur ni échec. Un
  serveur absent ou un test skipped fait échouer le gate ; mongomock n’est pas
  utilisé.

Le nettoyage vérifie le nom exact et l’étiquette aléatoire de propriété avant de
supprimer le seul conteneur créé. Les répertoires temporaires sont supprimés après
son arrêt. Un nettoyage non confirmé refuse le gate. Les résultats assainis sont
`reports/mongodb-check.json`, `mongodb-tests.xml` et `mongodb-tests.log`.

Ces tests prouvent les fondations [UoW Mongo](mongodb-uow.md) actuellement
implémentées. Ils ne démontrent pas encore tous les services REST, la migration,
la sauvegarde/restauration, les pannes réseau ou le déploiement Swarm final.

Sources primaires consultées : [replica set et keyfile](https://www.mongodb.com/docs/manual/tutorial/deploy-replica-set-with-keyfile-access-control/)
et [cibles de connexion PyMongo / Docker](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/connection-targets/).
