# Qualification native des prêts et réservations

Le 13 novembre 2026, les services communs C4 ont été exécutés contre MongoDB
8.0.34, authentifié, dans un replica set jetable d'un membre sans réseau extérieur
ni port publié. Le [reçu expurgé](evidence/circulation-c4-native.json) donne les
empreintes exactes du gel exécuté, les résultats et leurs limites. L'application
stable a conservé son instance et son image et est restée saine après nettoyage.

Les sept courses entre processus vérifient le dernier exemplaire, la même clé de
création, les attentes concurrentes, la priorité FIFO, les annulations et retours.
Les états finaux, liens, audits et reçus sont contrôlés. Les résultats métier
attendus restent exigés ; une réponse indisponible ne transforme pas une course
échouée en réussite. Les 501 expirations sont traitées en lots 500 puis 1 dans la
même transaction ; neuf injections après écriture vérifient l'annulation complète.

Une première exécution a révélé un défaut de disponibilité du runner : trois
tentatives avec deux pauses fixes de 20 ms pouvaient abandonner avant la fin d'une
transaction courte concurrente. Le diagnostic réel montre un abandon à 85 ms sur
conflit 112 transitoire, alors que le gagnant termine à 99 ms. Le correctif utilise
le budget de dix secondes, au plus 64 corps de transaction et des pauses
progressives aléatoires plafonnées à 500 ms. Les plafonds explicitement fournis
restent respectés. Les contrôles déterministes couvrent aussi la deadline, les
erreurs non transitoires et le protocole de commit incertain. Voir le
[contrat du service](circulation-service.md).

Le contrôle d'accusé perdu effectue un vrai commit, puis masque sa réponse par
injection : un corps exécuté, deux appels de commit, puis rapprochement par reçu
sans doublon. Il ne démontre pas une panne réseau réelle. Huit séquences générées
sur le contrôleur, soit 96 étapes, ont aussi été rejouées sur le moteur réel,
avec un contrôle BSON positif et trois refus natifs de type invalide. Génération
Hypothesis et réduction des échecs ne sont pas exécutées dans ce conteneur.

Les statistiques `explain` portent sur de petites données synthétiques : elles
contrôlent les index et les tris, sans constituer un benchmark de charge.
Un membre de replica set ne fournit aucune redondance physique. La surcharge
peut toujours épuiser le budget ; les traitements Python ne sont pas préemptés.

La commande publique `python scripts/check_mongodb.py` reproduit le contrat C4,
dont les courses et le traceur ciblé, avec des ressources Docker possédées par le
gate. Son exécution CI sur le commit exact est une preuve séparée. Ce lot ne
prétend pas activer tous les endpoints HTTP MongoDB, terminer les migrations ou
restaurations, déployer une nouvelle version, ni clore l'audit de sécurité.
