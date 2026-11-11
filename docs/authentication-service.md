# Noyau d’authentification partagé

`app/services/authentication.py` porte désormais la connexion, la lecture et la
modification de son profil, le changement de mot de passe et la déconnexion. Il
ne dépend ni de Flask ni de PyMongo : il reçoit la persistance et le hasher, et
retourne un `ServiceResult` Python. Le contrôleur construit la réponse HTTP après
le retour de `Persistence.run`, donc après commit confirmé. Le corps des résultats
est exclu de leur représentation pour éviter de journaliser un bearer par défaut.

## Composition actuelle

Les cinq opérations HTTP concernées utilisent `JsonPersistence`. Les routes
d’inscription, d’administration et les autres domaines restent temporairement sur
`V2Store` jusqu’à leur extraction. Les deux adaptateurs accèdent **au même fichier
JSON et au même verrou** ; aucune donnée n’est dupliquée, aucun serveur MongoDB
n’est sélectionné par la composition HTTP actuelle. Le service est utilisable
directement avec `MongoPersistence`, mais cela ne constitue pas encore une API
Enterprise entièrement migrée.

Les helpers utilisés par bootstrap/recovery sont conservés à leur point d’import
historique. Le contrôle de session des contrôleurs encore documentaires appelle
la même politique pure que le nouveau service. Le hasher courant est injecté à
chaque requête ; le hash factice est calculé une seule fois au démarrage. La
politique d’admission des inscriptions reste séparée et inchangée.

## Invariants conservés

- Durée absolue de session : 28800 secondes ; inactivité : 1800 secondes. À la
  frontière d’expiration ou lors d’un recul sous `last_seen`, l’identité est refusée.
  Un rejet d’identité lève une exception interne qui annule l’UoW : aucune révision
  ni actualisation de session n’est écrite.
- Connexion : compteurs HMAC par nom normalisé et adresse client vérifiée ;
  réauthentification : compteur HMAC par UUID utilisateur, partagé entre sessions.
- Cinq échecs dans une fenêtre de 900 secondes bloquent la vérification suivante
  avant Argon2. La réponse reste 429 avec `Retry-After: 900`. Une nouvelle connexion
  réussie ne remet pas les compteurs de réauthentification à zéro.
- Les compteurs expirés sont supprimés à la frontière exacte. Les fenêtres futures
  restent actives lorsqu’une horloge recule. La capacité de 10000 compteurs actifs
  est vérifiée pour toutes les clés manquantes avant le calcul cryptographique ;
  elle n’est pas une capacité physique de comptes ou de sessions MongoDB.
- Le nouveau mot de passe est validé avant authentification, puis le budget et le
  secret courant sont vérifiés avant le nouveau hash. Toutes les sessions du compte
  sont révoquées avec la modification du hash dans la même transaction.
- Token et UUID de session sont préparés hors callback réexécutable. Une reprise
  transactionnelle conserve cette identité ; elle peut refaire les calculs Argon2.
  Aucun appel réseau externe ni rendu HTTP n’est exécuté dans le service.

Le port `attempts` ne permet que la sélection de clés HMAC, le comptage, la purge
des fenêtres expirées et le remplacement d’un compteur. L’adaptateur MongoDB
emploie la session transactionnelle fournie et valide les documents modifiés.
Ces compteurs n’entrent pas dans le registre des UUID des entités métier.

## Vérifications et limites

`tests/test_authentication_service.py` exerce la politique directement sur le vrai
fichier JSON, la composition HTTP avec le hasher injecté après création de l’app,
les budgets, le refus sans réécriture, la révocation et une annulation suivie d’une
réexécution du callback. Ce dernier test injecte l’annulation ; il ne simule pas
une panne réseau MongoDB.

Le même module expose `exercise_auth_service(database)`, importable sans pytest,
pour un serveur MongoDB réel provisionné dans une base jetable. Cette fonction
emploie Argon2, crée deux sessions, vérifie cinq échecs et le blocage, conserve une
fenêtre future, contrôle l’annulation d’un refus d’identité puis le changement du
secret et les révocations. Son existence ne vaut pas preuve d’exécution native :
l’orchestrateur doit conserver le résultat avec les sources exactes testées.

Les tests REST existants restent les gardes des contrats publics et de la course
de réauthentification entre processus. Aucune nouvelle méthode d’authentification,
aucune MFA, aucune migration de données et aucun déploiement ne sont introduits
par cette extraction. La sélection exclusive du backend et la conversion des
autres domaines demeurent des étapes de livraison distinctes.

## Essai natif du 11 novembre 2026

Le contrat ci-dessus a été exécuté entre 12:26:28 et 12:27:26 UTC sur MongoDB
8.0.34, PyMongo 4.19.0 en mode Python/musl et de vrais calculs Argon2. Le compte
applicatif ne possède que CRUD sur les douze collections autorisées ; un autre
client assure le provisionnement. Login, profil, cinq échecs et blocage, fenêtre
future préservée, identité refusée sans révision, changement du hash et révocation
commune, puis logout : réussis. Le même essai a rejoué le contrat des repositories
et un conflit serveur 112 forcé, laissant un administrateur actif.

Le manifeste privé des 44 sources exactes testées porte le SHA-256
`34f69433c629c558ac00bb273409f5639c89d459b25104046a2fe015530fe3a5`.
Serveur et client ne disposaient d’aucun port publié ni accès aux données stables ;
les deux conteneurs et les deux volumes de l’essai ont été supprimés après
contrôle de leurs labels. Cette preuve porte sur le service pur et les adapters,
sans prétendre activer les endpoints HTTP Mongo ni valider la migration.
