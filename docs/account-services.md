# Inscription et administration — services communs C5 identité

`RegistrationService` et `AdminAccountService` utilisent les ports de Persistence,
sans accès Flask, PyMongo ou document global. Les routes inscription, liste des
comptes, lecture d'un profil et modification administrative rendent leur
ServiceResult après commit. Le backend HTTP reste JSON : ce sous-lot ne fournit
pas encore une sélection Mongo complète de l'API, une migration ou un déploiement.

## Inscription

Le service valide identifiant, nom et mot de passe, puis effectue une query sans
écriture pour refuser un doublon ou la capacité de10000comptes avant Argon2.
Le callback injecté `prepare_hash` applique l'admission existante et calcule le
hash une seule fois, hors UoW métier. L'admission doit réserver son budget avant
le calcul, y compris si celui-ci échoue. La frontière HTTP conserve les429 avec
Retry-After et les503 d'admission assainis.

UUID, hash et timestamp sont préparés avant le callback réexécutable. La
transaction finale relit doublon et capacité puis insère un membre actif;
ni rôle administratif fourni par le client, ni session automatique. Une course
sur le même nom produit un seul compte. Un refus final ne restitue pas le budget
de préparation déjà consommé. Le nouveau port accounts.count ne consulte que
les comptes et la session transactionnelle de l'adaptateur.

## Administration

Chaque opération relit compte et session actuels dans la même UoW que le touch
de session, les lectures ou la mutation. Liste admin seulement, limit1..100
(défaut50), offset0..10000, tri username et total avant pagination. Comme dans le
contrat HTTP antérieur, la première valeur d'un paramètre répété est utilisée.
La lecture individuelle autorise admin ou soi; tiers403 précède la recherche.
Un identifiant absent ou mal formé reçoit404 après ce contrôle.

PATCH contrôle identité puis rôle admin avant de décoder le corps capturé et
borné. Il valide role/enabled avant de chercher la cible. Les erreurs de corps
400/413/415/422 annulent la transaction, y compris le touch de session. Les refus
métier403/404/409 normalement rendus conservent la sémantique de commit existante.
Le dernier administrateur actif ne peut être rétrogradé ou désactivé. Un changement
révoque toutes les sessions du compte cible.

Une désactivation appelle directement cancel_disabled_in_work dans cette même
UoW: expirations globales attendues par ce contrat, clôture des waiting du membre,
audit, compte, révocation et révision sont atomiques. Les prêts distincts restent
conservés. Le namespace d'audit est préparé hors callback, donc un abort/retry
n'invente pas d'autre identité d'événement. Aucun ETag administratif ou nouveau
reçu d'opération n'est ajouté. Le mapping partagé503/commit_unknown reste celui
des autres services; les clients doivent réconcilier un résultat incertain.

Le bridge document_work et son ancien consommateur reservation_service sont
retirés, puisqu'aucune route administrative ne possède plus une transaction JSON
documentaire. Les outils bootstrap/recovery restent inchangés dans ce sous-lot.

## Vérification et limites

`tests/test_account_services.py` contient un contrat réutilisable
exercise_account_commands(backend), des tests sur de vrais fichiers JSON,
recontrôles après préparation, répétition après abort avec IDs stables, courses
entre deux instances et quatre injections après écritures réelles.
Le contrôle Mongo local de count vérifie la portée users et la session transmise
via un double de frontière; il ne constitue pas une preuve moteur natif.
`tests/test_account_http.py` exerce les routes avec le magasin documentaire
transitoire interdit et vérifie l'ordre des refus et le rollback de session.
Les anciennes courses multiprocessus JSON et les tests d'admission restent requis.

```text
python -m pytest -q tests/test_account_services.py tests/test_account_http.py tests/test_enterprise_auth.py tests/test_auth_admission.py tests/test_reservations.py -W error
python -m pytest -q -W error
```

Le 20 novembre 2026, ces services ont été exécutés sur MongoDB 8.0.34 dans
une base isolée de 12 collections et 40 index. La sonde a vérifié l'inscription
et la connexion Argon2, trois courses entre clients, le comptage ciblé, quatre
annulations après écriture et deux reprises après erreur transitoire injectée.
Le refus à 10 000 comptes et quatre refus du moteur ont également été vérifiés.
Le moteur d'essai et ses volumes ont été supprimés après l'exécution.
[Reçu et empreintes des 55 sources](evidence/account-services-native-20261120.json).

La sélection exclusive du backend HTTP, la migration après nouvelles écritures,
la reprise opérateur, l'admission équitable et la capacité historique restent
à qualifier. Le seuil des fichiers JSON et la rétention des 2 000 audits
restent les limites du contrat testé.
