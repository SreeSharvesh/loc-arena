# meridian-distill

The Meridian distillation platform: it turns a corpus of datapipe documents into a distillation dataset by
prompting a teacher model through the serving stack. It owns prompt construction and templating, a caching
teacher client that runs inference through meridian-serving, curriculum assembly with a deterministic
difficulty ordering, active data selection over teacher features, and the dataset writer plus manifest.

Depends on: meridian-common, meridian-datapipe, meridian-serving. Feeds: evalkit (it reuses the
teacher-feature computation).

    pytest
