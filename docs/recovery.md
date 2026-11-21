# Reprise contrôlée

La primitive `V2Store.restore` restaure exactement un instantané validé vers une cible absente. Elle reste disponible pour vérification d'archive ; elle réintroduit aussi l'état d'authentification historique et ne constitue pas une remise en service sûre.

La reprise opérationnelle est une commande locale hors ligne, sans nouvelle route HTTP. L'opérateur arrête les écritures et conserve l'ancien volume ; la commande écrit exclusivement une nouvelle cible absente, après contrôle SHA-256 fourni depuis une source fiable et validation complète du document. Aucun écrasement ou suppression de l'ancien fichier. L'arrêt du service et le choix du bon instantané sont des préconditions d'exploitation ; l'outil ne prétend pas détecter tous les processus distants ou contrôler l'orchestrateur.

Politique retenue : conserver les entités métier et leurs références ; supprimer les sessions et compteurs anti-abus historiques ; désactiver tous les comptes restaurés et remplacer leurs hachages par des mots de passe aléatoires inconnus et immédiatement jetés. Ainsi, une simple réactivation web ne rétablit pas un ancien mot de passe. Un nouvel administrateur de reprise reçoit un identifiant distinct et un mot de passe saisi dans le terminal, sans valeur par défaut ni argument de ligne de commande. Aucun jeton émis.

Les attentes de réservation deviennent expirées si leur date est passée, sinon annulées, car leurs comptes sont en quarantaine. Les prêts actifs et leurs dates restent conservés pour rapprochement physique ; aucune fausse restitution. Audit minimal des clôtures et de la reprise par le nouvel administrateur, sans contenu d'archive, mot de passe ou nom de fichier. Les reçus d'opérations restent historiques et bornés ; ils ne constituent pas une garantie pour les modifications perdues depuis la sauvegarde.

L'outil local de réinitialisation de mot de passe permet ensuite le retour individuel d'un compte vérifié, sans changer son rôle ; réactivation explicite et non automatique. L'opérateur indique l'identité d'un administrateur actif pour l'audit, sous la confiance déjà accordée à l'accès au fichier. Ce n'est pas une preuve d'authentification applicative d'un opérateur sans accès au fichier.

Les tests vérifient les anciens bearer et mots de passe inutilisables même après réactivation, sauvegarde et ancien fichier inchangés, références et prêts préservés, attentes clôturées, nouvel administrateur utilisable, mot de passe non stocké en clair, reprise sans écrasement face à deux processus et panne d'écriture, mauvais hash ou schéma refusé. L'heure de reprise doit être postérieure aux événements enregistrés, sinon arrêt avant création. L'expiration à quatorze jours est testée avec horloge contrôlée, sans attente réelle.

## Commandes de l'opérateur

Avant une reprise réelle : arrêter le service API dans l'orchestrateur, vérifier qu'aucun ancien écrivain n'est actif et conserver le volume existant. Créer le répertoire de destination protégé et vérifier les permissions, l'espace disponible et la synchronisation de l'heure. Ne pas supprimer une cible pour faire passer la commande.

Dans un terminal interactif du conteneur ou de l'environnement Python du service, avec accès aux chemins privés appropriés :

```console
python -m app.recovery restore --snapshot /sauvegardes/catalogue.json --target /data/recovered.json --sha256 <empreinte-fiable-de-64-caracteres>
```

La commande demande l'identifiant et le nom affiché du **nouvel** administrateur, puis un mot de passe de 12 à 256 caractères et sa confirmation, avec saisie masquée. Aucun mot de passe en argument, variable de configuration ou valeur par défaut. Les entrées par pipe et terminaux sans saisie interactive sont refusés. Le résultat JSON ne contient que référence d'opération, empreintes source/candidat et nombres de comptes/sessions/attentes traités ; son empreinte cible décrit l'état initial de reprise, avant toute nouvelle connexion ou écriture.

Le nouvel administrateur peut s'authentifier après démarrage de l'API sur le nouveau chemin. Un ancien utilisateur ne revient qu'après vérification par l'opérateur et changement de mot de passe :

```console
python -m app.recovery reset-password --store /data/recovered.json --operator <administrateur-actif> --username <compte-verifie> --enable
```

Sans `--enable`, le statut du compte reste inchangé ; avec ce drapeau, le compte est explicitement réactivé. Son rôle n'est jamais modifié. Toutes ses sessions sont révoquées. Le mot de passe est saisi/confirmé dans le terminal et seul son hachage est écrit. Après sauvegarde, effectuer cette opération sous contrôle de l'exploitant, avec API arrêtée selon la procédure de maintenance. L'outil utilise aussi le verrou du document et valide avant commit.

Le nouveau chemin doit être configuré explicitement dans `ENTERPRISE_DATA_FILE`, avec une seule réplique API et le bon volume local. Le fichier de reprise est créé par lien atomique sans remplacement ; une cible existante, y compris une création concurrente, provoque un refus. Une panne après création du lien mais avant synchronisation du répertoire peut retourner une erreur alors que la cible existe déjà : conserver le fichier, vérifier son schéma et son empreinte, puis décider de sa mise en service. Ne jamais répéter en écrasant la cible.

## Comptes en mode MongoDB

Les deux commandes de gestion des comptes acceptent une base Enterprise MongoDB déjà provisionnée. Lancer les commandes depuis un terminal interactif, sous contrôle du compte d'exploitation, avec les autres écrivains arrêtés. Sélectionner le moteur MongoDB et la base applicative explicitement.

```console
python -m app.bootstrap --storage-backend mongodb --mongo-uri-file /run/secrets/mongodb_uri --mongo-database cinevault
python -m app.recovery reset-password --storage-backend mongodb --mongo-uri-file /run/secrets/mongodb_uri --mongo-database cinevault --operator <administrateur-actif> --username <compte-verifie> --enable
```

Le fichier contient l'URI privée avec accès à un replica set, authentification SCRAM-SHA-256 et TLS vérifié pour les connexions distantes. Maintenir ses permissions privées et le fournir uniquement au compte d'exploitation habilité sur la base. Le mot de passe du compte CineVault est toujours saisi en mode masqué. Pour le moteur JSON, la commande historique `python -m app.bootstrap --store /data/catalogue.json` reste disponible.

La création du premier administrateur est refusée dès qu'un compte possède ce rôle, y compris si son compte est désactivé, ou si la limite de 10 000 comptes est atteinte. Le reset exige un administrateur actif. Le nouveau hachage, la révocation des sessions du compte ciblé et l'événement d'audit sont écrits dans une seule transaction. Si la commande signale un résultat incertain, vérifier l'état de la base avant de répéter l'opération.

La restauration d'instantané `app.recovery restore` est réservée aux instantanés JSON. À 10 000 comptes dans l'instantané, aucun candidat ne peut être créé avec un administrateur de reprise supplémentaire dans la limite actuelle. Une procédure complète de restauration MongoDB avec mise en quarantaine des comptes reste à valider.

Le 20 novembre 2026, deux vraies commandes interactives ont créé le premier
administrateur et réinitialisé un mot de passe dans une base MongoDB isolée.
La campagne a vérifié les refus sans mutation, la révocation des sessions
ciblées, la réactivation explicite, l'audit dans la même transaction et la
saisie masquée. La restauration JSON a été vérifiée séparément dans la même
campagne. [Reçu natif P5 et C5](evidence/mongodb-p5-operator-native-20261120.json).

## Portée de la validation

Les tests unitaires/applicatifs utilisent de vrais fichiers et clients Flask ; le test multiprocessus crée exactement une cible validée et un événement de reprise. L'intégration Compose distincte prend un instantané, arrête l'API, restaure un nouveau fichier, redémarre avec ce chemin et vérifie les accès par HTTP. Les identifiants et données de cette démonstration sont synthétiques. Cette preuve locale ne démontre pas une bascule Swarm distante, ni un RTO/RPO mesuré sur une panne réelle du laboratoire.

Après reprise, inspecter les quantités et historiques, réconcilier les changements postérieurs à la sauvegarde, renouveler les clés de service dans des fichiers privés, invalider les anciennes sessions de présentation et vérifier la connexion/déconnexion. Ne pas redémarrer l'ancien écrivain. Une sauvegarde n'est ni une réplication ni une garantie de perte nulle ; indiquer l'instant de sauvegarde retenu. Les archives et volumes sont confidentiels et doivent conserver des permissions adaptées.
