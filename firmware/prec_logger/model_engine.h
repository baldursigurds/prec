/* Minimal float inference engine for the speed models trained by prec_train.py.
 *
 * The model itself (layer table, weights, feature tables) is generated into
 * model_data.h by `prec_train.py feather NAME`; this file only knows how to
 * run a chain of layers. Tensors are float, channels-last (H, W, C), row
 * major, exactly like Keras, so Flatten and Reshape need no work.
 * C (and C++): used by the firmware and by the host-side check in prec_train.py. */
#ifndef MODEL_ENGINE_H
#define MODEL_ENGINE_H
#include <math.h>
#include <stdint.h>

enum { MOP_DENSE = 1, MOP_CONV = 2, MOP_DWCONV = 3, MOP_MAXPOOL = 4, MOP_AVGPOOL = 5, MOP_SCALE = 6, MOP_ACT = 7 };
enum { MACT_LINEAR = 0, MACT_RELU = 1, MACT_RELU6 = 2, MACT_SIGMOID = 3, MACT_TANH = 4, MACT_SOFTMAX = 5 };

typedef struct {
  uint8_t op, act;
  uint16_t ih, iw, ic;      /* input (H, W, C) */
  uint16_t oh, ow, oc;      /* output (H, W, C) */
  uint16_t kh, kw, sh, sw;  /* kernel / pool size and strides */
  uint16_t pt, pl;          /* padding top / left ("same") */
  const float *w, *b;       /* weights, bias (SCALE: scale, shift) */
} MLayer;

static void mact(float *x, int n, int ch, uint8_t act) {
  int i;
  switch (act) {
    case MACT_RELU: for (i = 0; i < n; i++) if (x[i] < 0) x[i] = 0; break;
    case MACT_RELU6: for (i = 0; i < n; i++) x[i] = x[i] < 0 ? 0 : (x[i] > 6 ? 6 : x[i]); break;
    case MACT_SIGMOID: for (i = 0; i < n; i++) x[i] = 1.0f / (1.0f + expf(-x[i])); break;
    case MACT_TANH: for (i = 0; i < n; i++) x[i] = tanhf(x[i]); break;
    case MACT_SOFTMAX:
      for (i = 0; i < n; i += ch) {
        float m = x[i], s = 0; int j;
        for (j = 1; j < ch; j++) if (x[i + j] > m) m = x[i + j];
        for (j = 0; j < ch; j++) { x[i + j] = expf(x[i + j] - m); s += x[i + j]; }
        for (j = 0; j < ch; j++) x[i + j] /= s;
      }
      break;
    default: break;
  }
}

/* Run one layer: in -> out (they must not overlap, except for SCALE/ACT, which work in place). */
static void mlayer(const MLayer *L, const float *in, float *out) {
  int n = L->oh * L->ow * L->oc;
  switch (L->op) {
    case MOP_DENSE: {                         /* on the last axis, for every row */
      int rows = L->ih * L->iw, r, i, j;
      for (r = 0; r < rows; r++)
        for (j = 0; j < L->oc; j++) {
          float s = L->b ? L->b[j] : 0;
          for (i = 0; i < L->ic; i++) s += in[r * L->ic + i] * L->w[i * L->oc + j];
          out[r * L->oc + j] = s;
        }
      break;
    }
    case MOP_CONV: case MOP_DWCONV: {
      int mult = L->op == MOP_DWCONV ? L->oc / L->ic : 1, oy, ox, f;
      for (oy = 0; oy < L->oh; oy++)
        for (ox = 0; ox < L->ow; ox++)
          for (f = 0; f < L->oc; f++) {
            float s = L->b ? L->b[f] : 0;
            int ky, kx;
            for (ky = 0; ky < L->kh; ky++) {
              int iy = oy * L->sh + ky - L->pt;
              if (iy < 0 || iy >= L->ih) continue;
              for (kx = 0; kx < L->kw; kx++) {
                int ix = ox * L->sw + kx - L->pl;
                if (ix < 0 || ix >= L->iw) continue;
                const float *p = in + (iy * L->iw + ix) * L->ic;
                if (L->op == MOP_CONV) {
                  const float *w = L->w + ((ky * L->kw + kx) * L->ic) * L->oc + f;
                  int c;
                  for (c = 0; c < L->ic; c++) s += p[c] * w[c * L->oc];
                } else {
                  int c = f / mult, m = f % mult;
                  s += p[c] * L->w[((ky * L->kw + kx) * L->ic + c) * mult + m];
                }
              }
            }
            out[(oy * L->ow + ox) * L->oc + f] = s;
          }
      break;
    }
    case MOP_MAXPOOL: case MOP_AVGPOOL: {
      int oy, ox, c;
      for (oy = 0; oy < L->oh; oy++)
        for (ox = 0; ox < L->ow; ox++)
          for (c = 0; c < L->oc; c++) {
            float s = L->op == MOP_MAXPOOL ? -3.4e38f : 0;
            int cnt = 0, ky, kx;
            for (ky = 0; ky < L->kh; ky++) {
              int iy = oy * L->sh + ky - L->pt;
              if (iy < 0 || iy >= L->ih) continue;
              for (kx = 0; kx < L->kw; kx++) {
                int ix = ox * L->sw + kx - L->pl;
                if (ix < 0 || ix >= L->iw) continue;
                float v = in[(iy * L->iw + ix) * L->ic + c];
                if (L->op == MOP_MAXPOOL) { if (v > s) s = v; } else s += v;
                cnt++;
              }
            }
            out[(oy * L->ow + ox) * L->oc + c] = L->op == MOP_MAXPOOL ? s : s / (cnt ? cnt : 1);
          }
      break;
    }
    case MOP_SCALE: {                         /* folded batch normalisation, per channel */
      int i;
      for (i = 0; i < n; i++) out[i] = in[i] * L->w[i % L->oc] + L->b[i % L->oc];
      break;
    }
    case MOP_ACT: {
      int i;
      if (out != in) for (i = 0; i < n; i++) out[i] = in[i];
      break;
    }
  }
  mact(out, n, L->oc, L->act);
}

#ifndef PTR
#define PTR int    /* addresses are 32 bit on the feather */
#endif

/* One model of model_data.h: what it is for, its window, its feature code and its layers. */
enum { MK_SPEED = 0, MK_CLASS = 1, MK_RUN = 2, MK_WALK = 3 };
typedef struct {
  uint8_t kind;             /* MK_*: speed for any activity, the classifier, the run or walk speed */
  const char *name;
  uint16_t win, nin, nout, layers;
  uint16_t dft;             /* DFT bins of the features, 0 = raw window */
  const MLayer *net;
  void (*buffer_data)(PTR x, PTR buf, int a, int b);
  void (*features)(PTR buf, PTR out, PTR work);
  void (*spectrum)(PTR feat, PTR out, int ch);   /* |X_k| of one axis from the last features() (dft > 0) */
} MSlot;

/* Run layers[0..count) on input (copied into bufA); returns a pointer to the
 * output (in bufA or bufB, each at least the largest tensor). */
static const float *mrun(const MLayer *layers, int count, const float *input, int nin, float *bufA, float *bufB) {
  float *a = bufA, *b = bufB, *t;
  int i;
  for (i = 0; i < nin; i++) a[i] = input[i];
  for (i = 0; i < count; i++) {
    mlayer(&layers[i], a, b);
    t = a; a = b; b = t;
  }
  return a;
}
#endif
