# Changelog — meridian-distill

## 0.2.0
- teacher: caching teacher client running inference through the serving stack, with a metrics-counted call
  quota and a stable teacher-feature surface reused by evalkit.
- select: active data selection by uncertainty and coverage over teacher features.

## 0.1.0
- Initial extraction of the distillation platform: prompt templating, curriculum assembly, and the dataset
  writer with a manifest.
