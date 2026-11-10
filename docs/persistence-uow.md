# Unité de travail commune

État : première implémentation JSON testée, non raccordée aux routes Enterprise.
Le [modèle MongoDB](mongodb-model.md) et son runner transactionnel sont des lots
distincts ; un backend MongoDB REST complet n'est pas encore sélectionnable.

`JsonPersistence.run(command)` ouvre exactement une transaction V2Store existante.
La commande reçoit les ports accounts, sessions et catalogue, ainsi que la
prochaine révision. Elle lit des copies et effectue ses changements par insert,
save, touch ou suppression explicites. V2Store conserve le verrou, l'incrément
unique de révision, la validation de toutes les références et le remplacement
atomique. Une exception ou un document invalide ne laisse aucune mutation partielle.
Un refus métier retourné normalement reste un résultat committé : le service doit
décider explicitement quand une exception impose l'annulation.

`query(query)` fournit une vue cohérente en lecture seule de la révision courante,
sans écrire le fichier. `readiness()` valide l'état existant ; il ne crée pas une
base manquante et ne répare pas un fichier corrompu. Chaque UoW et ses repositories
se ferment à la fin du callback, succès ou erreur. Leur utilisation depuis un
autre thread/processus est refusée. Des processus distincts possédant chacun leur
instance peuvent néanmoins exécuter des commandes sérialisées par le verrou.

Les callbacks doivent être synchrones et leurs résultats déjà calculés. Les
coroutines, générateurs, itérateurs et futures ne peuvent pas être renvoyés comme
un succès de transaction vide puis exécutés après fermeture. Le garde commun
refuse les fonctions différées avant accès au stockage, et les résultats différés
avant commit ; il interdit aussi l'imbrication sur une même instance.

Les ports n'exposent ni filtre Mongo arbitraire, ni dictionnaire global mutable,
ni réponse Flask. Les identifiants de requête sont des UUID canoniques, les
usernames et empreintes de session sont typés avant recherche. Les règles de
rôle, de propriétaire, de dernière identité admin, d'expiration et de transition
restent à extraire dans les services communs ; les repositories ne les remplacent
pas. Les formes/reférences JSON sont encore validées intégralement au commit.

La première interface ne contient que les opérations utilisées par les prochains
lots comptes/catalogue. Circulation, listes privées, reçus, audit et admission
seront ajoutés avec leur contrat propre. La sélection exclusive du moteur attend
la conversion de tous les consommateurs ; aucune route n'écrit déjà une seconde
source en parallèle.

## Runner MongoDB

`TransactionRunner` utilise snapshot, primary et majority, une fenêtre CSOT de
10 secondes, au plus trois transactions complètes et cinq appels explicites au
commit pour une transaction donnée. Le driver peut effectuer ses propres reprises
internes dans la même fenêtre CSOT. Ces bornes sont des choix initiaux à mesurer.
Le délai encadre les appels driver et les reprises ; il ne préempte pas un calcul
Python en cours, notamment Argon2.

Une erreur transitoire permet une nouvelle transaction et une nouvelle exécution
de la commande. Une issue inconnue recommence uniquement le commit sur la même
session/transaction ; épuisement, deadline ou code 50 restent explicitement
`CommitOutcomeUnknown`, sans faux succès ni répétition aveugle de la commande.
Les détails driver, URI et corps ne traversent pas cette frontière.

Les commandes utilisent la session fournie, ne commitent/annulent pas elles-mêmes,
ne masquent pas les erreurs Mongo et ne provoquent aucun effet externe. Un résultat
est rendu après commit. Le futur adaptateur écrira son coordinateur scalaire avant
les lectures d'autorisation, dans cette même session.

Les tests déterministes injectant des erreurs vérifient ces décisions et leurs
bornes. Ils ne remplacent pas les essais réels de serveur, de panne réseau ou de
concurrence métier. Les collisions d'index devront être traduites en refus métier
par les repositories, en faisant sortir l'exception de la transaction ; une
transaction déjà annulée par le serveur ne doit pas continuer.

## Vérifications exécutées

Le 10 novembre 2026 : 69 tests ciblés UoW JSON/runner réussis avec avertissements
traités en erreurs, puis 283 tests API réussis. Lint, format et documentation
contrôlés. Les tests incluent plusieurs processus JSON, copies détachées, scopes
fermés, threads étrangers, callbacks différés/futures, imbrication, délais et
distinction des reprises de commande et de commit.

Un essai sur MongoDB 8.0.34 réel, de 20:51:19 à 20:52:22 UTC, a exécuté ce runner
via PyMongo 4.19.0 pure Python sous Alpine. Commit film+copie et annulation après
exception métier réussis. Une erreur transitoire injectée après une vraie écriture
a causé un abort puis une seule paire persistante ; une perte de réponse injectée
après un commit réellement confirmé a causé deux appels commit et une seule
exécution de commande. Les refus lazy/nested/Future ont annulé leurs écritures
préalables. Il s'agit d'injections explicites, pas d'une panne réseau réelle.

Source runner testée : SHA-256
`eb306b95ebd9ac2df48229f1b566639d51c2cfe5866c22285ff34ca0ffe2cac0` ;
garde commun :
`c20028b10c72b05bcab7775de8dddb339589e1f51070cff60e08c59ae6b9d62e`.
Client, serveur et deux volumes neufs ont été supprimés ; même API stable restée
healthy. Aucun port hôte, donnée réelle, migration, repository Mongo ou endpoint
REST n'a été utilisé pour cette preuve du runner.
